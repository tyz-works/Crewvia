#!/usr/bin/env bash
set -euo pipefail

# plan.sh — Crewvia タスクプラン管理 CLI（per-task / multi-mission 版）
#
# Layout:
#   queue/
#     state.yaml                    active mission slugs + default_mission
#     missions/<slug>/
#       mission.yaml                title / status / next_task_id
#       tasks/tNNN.md               frontmatter + body (Description / Result)
#     archive/                      完了済み mission の退避先
#     .lock                         fcntl 排他ロックファイル
#
# Usage:
#   plan.sh init "<title>" [--mission <slug>] [--force]
#   plan.sh add  "<title>" [--mission <slug>] --skills <csv> [--blocked-by <csv>]
#                          [--priority high|medium|low] [--description <text>]
#   plan.sh pull [--mission <slug>] [--skills <csv>] [--agent <name>] [--target-dir <path>]
#                [--task <task_id>]
#                              --skills 省略時は環境変数 SKILLS → registry の Worker の skills の順。
#                              どれも無ければ拒否 (skill の絞り込みを丸ごと無効にしない)。
#                              Director (registry の role: director) は pull できない
#   plan.sh done <task_id> "<result>" [--mission <slug>]
#   plan.sh fail <task_id> [<handoff_path>] (--head <sha> | --no-head <理由>) [--mission <slug>]
#   plan.sh update <task_id> [--mission <slug>] [--skills <csv>] [--blocked-by <csv>]
#                            [--priority high|medium|low] [--worker <name>] [--status <status>]
#                            [--description <text>] [--reset]
#   plan.sh release-dep <task_id> [--dep <csv>] [--mission <slug>]
#                              failed の依存で保留されている task を、Director が明示的に
#                              進めてよいと決める (省略時は今 failed の依存すべて)。
#                              依存の辺は消えず、card に released_deps として残る
#   plan.sh retire <task_id> --agent <name> --started-at <generation>
#                            [--mission <slug>] [--outcome reset|needs-director]
#                            [--reason "<1 行>"] [--no-wait]
#                              実行アイデンティティで束縛した後始末。前提が外れたら
#                              1 バイトも書かずに exit 3 (詳細は cmd_retire)
#                              --no-wait: キューロックを待たずに諦め exit 4。
#                              待てない常駐デーモン (watchdog) 用
#   plan.sh task-graph          herdr-task-graph 用の tasks.json を書き出す
#                              （queue は変更しない。普段は queue を書き換える
#                                サブコマンドが自動で呼ぶ）
#   plan.sh status [--mission <slug>] [--all]
#   plan.sh resolve-mission <task_id> [--mission <slug>]
#                              task が属する mission の slug を 1 行出す (読み取り専用。
#                              --mission 省略時の探索順は pull と同じ)
#   plan.sh archive <slug>
#   plan.sh dashboard [--all]   fzf/gum の TUI (bash 側で完結。引数の規則は他と同じ)
#
# 引数は厳格: 未知の option (`-x` / `--xxx`) と余った positional は usage を出して exit 2
# (`pull` だけ exit 1 — pull の 2 は「タスクなし」)。`--` 以降は positional。
# `-h` / `--help` は usage を出して exit 0 で、queue にも registry にも何も書かない。
# done / fail / needs-director / update で --mission を省略して task id が複数 mission に
# 当たるとき、CREWVIA_MISSION_SLUG の mission に自分 (AGENT_NAME) が in_progress で担当している
# 場合に限ってそれを使う (stderr に 1 行出す)。それ以外は拒否して候補とコマンドを示す。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUEUE_DIR="${CREWVIA_QUEUE:-${REPO_ROOT}/queue}"

if [[ $# -eq 0 ]]; then
  echo "Usage: plan.sh <init|add|pull|done|needs-director|fail|update|release-dep|retire|ready-for-verification|verify-result|review|launch|task-graph|lint|status|archive|resync|dashboard|dashboard-data|resolve-mission> [args...]" >&2
  exit 1
fi

SUBCOMMAND="$1"
shift

# `plan.sh -h` / `--help`: 何も書かずに usage を出す。サブコマンドの位置に `--help` を置いたとき
# 「--help という名前の subcommand」として扱わず (init --help が「--help」mission を作った事故)、
# queue の骨組みも作らない (骨組みは引数を検証し終えた python 側 `_ensure_queue_dirs()` が作る)。
if [[ "$SUBCOMMAND" == "-h" || "$SUBCOMMAND" == "--help" || "$SUBCOMMAND" == "help" ]]; then
  sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

# `dashboard` は python の dispatch を通らず bash 側で完結する (fzf/gum の TUI) ので、parse_opts と
# 同じ規則をここで掛ける: `-h` / `--help` は usage を出して exit 0、未知の option と positional は
# exit 2。**どちらの場合も queue の骨組みは作らない** (mkdir は検証を通ったあと)。
# 検証を bash 側の分岐に持たなかった頃は、`dashboard --help` が queue/ を作って TUI を起動し、
# `dashboard --bogus` が受理された (t006 QA の FAIL)。
if [[ "$SUBCOMMAND" == "dashboard" ]]; then
  _dashboard_usage_exit() {
    echo "plan.sh dashboard: $1" >&2
    echo "Usage: plan.sh dashboard [--all]" >&2
    exit 2
  }
  _dashboard_option_like='^--?[A-Za-z][A-Za-z0-9_-]*(=.*)?$'
  _dashboard_after_dd=0
  for _dashboard_arg in "$@"; do
    if [[ "$_dashboard_after_dd" -eq 1 ]]; then
      _dashboard_usage_exit "expected 0 positional argument(s), got '$_dashboard_arg'"
    fi
    case "$_dashboard_arg" in
      --) _dashboard_after_dd=1 ;;
      -h|--help) echo "Usage: plan.sh dashboard [--all]"; exit 0 ;;
      --all) ;;
      *)
        if [[ "$_dashboard_arg" =~ $_dashboard_option_like ]]; then
          _dashboard_usage_exit "unknown option '$_dashboard_arg'"
        fi
        _dashboard_usage_exit "expected 0 positional argument(s), got '$_dashboard_arg'"
        ;;
    esac
  done
  unset -f _dashboard_usage_exit
  unset _dashboard_option_like _dashboard_arg _dashboard_after_dd
  # 検証を通ったあとにだけ骨組みを作る
  mkdir -p "$QUEUE_DIR" "$QUEUE_DIR/missions" "$QUEUE_DIR/archive"
fi

# ─── dashboard TUI (fzf + gum) ───────────────────────────────────────────────

_plan_dashboard() {
  local show_all=""
  for arg in "$@"; do [[ "$arg" == "--all" ]] && show_all="--all"; done

  for dep in fzf gum jq; do
    command -v "$dep" &>/dev/null \
      || { echo "Error: '$dep' not installed. Run: brew install $dep" >&2; return 1; }
  done

  local self="$SCRIPT_DIR/plan.sh"
  local tmpdata tmppreview_m tmppreview_t
  tmpdata=$(mktemp /tmp/crewvia-dash-XXXX.json)
  tmppreview_m=$(mktemp /tmp/crewvia-preview-m-XXXX.sh)
  tmppreview_t=$(mktemp /tmp/crewvia-preview-t-XXXX.sh)
  chmod +x "$tmppreview_m" "$tmppreview_t"
  # shellcheck disable=SC2064
  trap "rm -f '$tmpdata' '$tmppreview_m' '$tmppreview_t'" EXIT

  cat > "$tmppreview_m" << 'PREVEOF'
#!/bin/bash
SLUG="$1"; DATA="$2"
jq -r --arg s "$SLUG" '
  (.missions + .archived) | map(select(.slug == $s)) | .[0] // {} |
  "Tasks: \(.done // 0)/\(.total // 0)" + "\n" +
  ((.tasks // []) |
    if length == 0 then "  (no tasks)"
    else [.[] |
      "  " + (if .status == "done" then "[32m✅"
         elif .status == "in_progress" then "[33m🔄"
         elif .status == "blocked" then "[31m🚫"
         else "[37m⏳" end) +
      " \(.id) \(.title)[0m" +
      (if .worker then " (\(.worker))" else "" end)
    ] | join("\n")
    end)
' "$DATA"
PREVEOF

  cat > "$tmppreview_t" << 'PREVEOF'
#!/bin/bash
TASK_ID="$1"; SLUG="$2"; DATA="$3"
jq -r --arg s "$SLUG" --arg t "$TASK_ID" '
  (.missions + .archived) | map(select(.slug == $s)) | .[0].tasks // [] |
  map(select(.id == $t)) | .[0] // {} |
  "Status:    \(.status // "?")\n" +
  "Worker:    \(.worker // "unassigned")\n" +
  "Priority:  \(.priority // "?")\n" +
  "Skills:    \((.skills // []) | join(", "))\n" +
  (if ((.blocked_by // []) | length) > 0 then "Blocked:   \(.blocked_by | join(", "))\n" else "" end) +
  "\n── Description ──\n" +
  (.description // "(no description)")
' "$DATA"
PREVEOF

  local refresh=1
  while [[ "$refresh" -eq 1 ]]; do
    refresh=0
    "$self" dashboard-data $show_all 2>/dev/null > "$tmpdata" \
      || { echo "Failed to load mission data." >&2; return 1; }

    local lines
    lines=$(jq -r '
      (.missions + .archived) | .[] |
      (if .archived then "[90m📦"
       elif .status == "done" then "[32m✅"
       elif .status == "in_progress" then "[33m🔄"
       elif .status == "blocked" then "[31m🚫"
       elif .status == "drafting" then "[36m📝"
       else "[37m⏳" end) as $icon |
      "\(.slug)\t\($icon) \(.title) [\(.done)/\(.total)][0m"
    ' "$tmpdata")

    if [[ -z "$lines" ]]; then
      gum style --foreground 240 "No missions found."
      return 0
    fi

    local result key slug
    result=$(printf '%s\n' "$lines" | fzf \
      --ansi \
      --delimiter=$'\t' \
      --with-nth='2..' \
      --prompt='  Mission > ' \
      --header=$'  Enter=Select  r=Refresh  q=Quit' \
      --expect='r,q' \
      --preview="$tmppreview_m {1} $tmpdata" \
      --preview-window='right:50%:wrap' \
      2>/dev/null) || return 0

    key=$(printf '%s' "$result" | head -1)
    slug=$(printf '%s' "$result" | tail -n +2 | cut -f1)

    if [[ "$key" == "q" ]]; then return 0; fi
    if [[ "$key" == "r" ]]; then refresh=1; continue; fi
    [[ -z "$slug" ]] && continue

    _dashboard_tasks "$self" "$slug" "$show_all" "$tmpdata" "$tmppreview_t"
  done
}

_dashboard_tasks() {
  local self="$1" slug="$2" show_all="$3" tmpdata="$4" tmppreview_t="$5"

  while true; do
    "$self" dashboard-data $show_all 2>/dev/null > "$tmpdata" || true

    local lines
    lines=$(jq -r --arg s "$slug" '
      (.missions + .archived) | map(select(.slug == $s)) | .[0].tasks // [] | .[] |
      (if .status == "done" then "[32m✅"
       elif .status == "in_progress" then "[33m🔄"
       elif .status == "blocked" then "[31m🚫"
       else "[37m⏳" end) as $icon |
      "\(.id)\t\($icon) \(.title)[0m" + (if .worker then " (\(.worker))" else "" end)
    ' "$tmpdata")

    if [[ -z "$lines" ]]; then
      gum style --foreground 240 "No tasks in this mission."
      sleep 1
      return 0
    fi

    local result key task_id
    result=$(printf '%s\n' "$lines" | fzf \
      --ansi \
      --delimiter=$'\t' \
      --with-nth='2..' \
      --prompt="  Task ($slug) > " \
      --header=$'  Enter=Detail  r=Refresh  q/Esc=Back' \
      --expect='r,q' \
      --preview="$tmppreview_t {1} $slug $tmpdata" \
      --preview-window='right:50%:wrap' \
      2>/dev/null) || return 0

    key=$(printf '%s' "$result" | head -1)
    task_id=$(printf '%s' "$result" | tail -n +2 | cut -f1)

    if [[ "$key" == "q" ]]; then return 0; fi
    if [[ "$key" == "r" ]]; then continue; fi
    [[ -z "$task_id" ]] && return 0

    _dashboard_task_detail "$slug" "$task_id" "$tmpdata"
  done
}

_dashboard_task_detail() {
  local slug="$1" task_id="$2" tmpdata="$3"

  local info
  info=$(jq -c --arg s "$slug" --arg t "$task_id" '
    (.missions + .archived) | map(select(.slug == $s)) | .[0].tasks // [] |
    map(select(.id == $t)) | .[0] // {}
  ' "$tmpdata")

  local title status worker priority skills description color
  title=$(jq -r '.title // "?"' <<< "$info")
  status=$(jq -r '.status // "?"' <<< "$info")
  worker=$(jq -r '.worker // "unassigned"' <<< "$info")
  priority=$(jq -r '.priority // "?"' <<< "$info")
  skills=$(jq -r '(.skills // []) | join(", ")' <<< "$info")
  description=$(jq -r '.description // "(no description)"' <<< "$info")

  case "$status" in
    done)        color=2;;
    in_progress) color=3;;
    blocked)     color=1;;
    *)           color=8;;
  esac

  clear
  gum style \
    --border rounded --border-foreground "$color" \
    --padding "1 2" --width 72 \
    "$(gum style --bold "$task_id: $title")" \
    "" \
    "Status:   $(gum style --foreground "$color" "$status")" \
    "Worker:   $worker" \
    "Priority: $priority" \
    "Skills:   $skills" \
    "" \
    "$(gum style --bold 'Description')" \
    "$description"

  echo ""
  read -rp "  [Press Enter to go back] " < /dev/tty || true
}

# ─────────────────────────────────────────────────────────────────────────────

if [[ "$SUBCOMMAND" == "dashboard" ]]; then
  _plan_dashboard "$@"
  exit $?
fi

# Delegate to Python3
python3 - "$QUEUE_DIR" "$SUBCOMMAND" "$REPO_ROOT" "$@" <<'PYEOF'
import sys
import os
import json
import fcntl
import contextlib
import re
import shutil
import hashlib
import subprocess
import shlex
import time
import urllib.request
from datetime import datetime, timezone

QUEUE_DIR = sys.argv[1]
SUBCOMMAND = sys.argv[2]
REPO_ROOT = sys.argv[3]
ARGS = sys.argv[4:]

STATE_FILE = os.path.join(QUEUE_DIR, 'state.yaml')
MISSIONS_DIR = os.path.join(QUEUE_DIR, 'missions')
ARCHIVE_DIR = os.path.join(QUEUE_DIR, 'archive')
LOCK_FILE = os.path.join(QUEUE_DIR, '.lock')


# ---------------------------------------------------------------------------
# scripts/ の共有モジュール
# ---------------------------------------------------------------------------
#
# 読み込み元は REPO_ROOT (= 実行された plan.sh 自身の置き場) であって
# CREWVIA_REPO_ROOT ではない。コードは、今走っている plan.sh と同じ checkout
# から来なければならない — worktree の plan.sh が本体のコードを読むと、
# worktree で直したはずの規則が効かない。
#
# 失敗はそのまま外に出す。try で包んで自前の実装に落ちると、「規則は 1 箇所」
# という性質が壊れた環境でだけ静かに失われる。plan.sh が起動しないほうがまだよい。

def _load_scripts_module(name):
    """`scripts/<name>.py` を読み込む。失敗はそのまま外に出す。"""
    import importlib.util, pathlib
    path = pathlib.Path(REPO_ROOT) / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'共有モジュールを読めません: {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# task カードの読み取り —— parser・識別子・隔離の規則は 1 箇所しかない。
# dispatcher.sh も同じモジュールを読む: 同じ queue を 2 つの別のコードが別の
# 規則で読んでいたのが Codex 5 巡目 P2 の指摘で、そのときズレは「pull は
# 受理するのに dispatch サイクルが KeyError で落ちる」という形で出た。
# 再発防止は tests/test_task_card_identity.py。
_TASK_CARDS = _load_scripts_module('lib_task_cards')
parse_yaml = _TASK_CARDS.parse_yaml
parse_frontmatter = _TASK_CARDS.parse_frontmatter
_scalar = _TASK_CARDS._scalar
_split_inline_list = _TASK_CARDS._split_inline_list

PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'verified', 'skipped'}

# Pseudo-status for a task file that failed to parse (see lib_task_cards). Never
# 'pending', so pull/dispatch skip it automatically; never in
# TERMINAL_STATUSES, so a mission with a corrupted task is never mistaken for
# complete. It exists purely so ONE malformed tNNN.md cannot take the rest of
# the mission down with it (t009: a multi-line needs-director reason broke
# frontmatter parsing and froze plan.sh status / dispatch entirely).
CORRUPT_TASK_STATUS = _TASK_CARDS.CORRUPT_TASK_STATUS

STATUS_ICON = {
    'done': '✅',
    'verified': '✅',
    'in_progress': '🔄',
    'pending': '📋',
    'skipped': '⏭️',
    'failed': '❌',
    'ready_for_verification': '🔍',
    'verifying': '🔎',
    'verification_failed': '⚠️',
    'needs_human_review': '👁️',
    'needs_director': '🆘',
    CORRUPT_TASK_STATUS: '💥',
}


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def now_generation():
    """`started_at` に入れる実行世代。

    now_iso() の秒精度では、差し戻し直後の再 pull が同じ秒に収まった瞬間に
    先任と後任の started_at が文字列として一致し、「別の実行」を区別できなく
    なる。世代として突き合わせる値である以上、衝突してはいけないので小数秒まで
    刻む (RFC3339 で valid、timezone は同じく UTC)。

    値としては不透明な識別子であり、時刻として演算される想定ではない。
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# ---------------------------------------------------------------------------
# Minimal YAML helpers (narrow subset, no external deps)
# ---------------------------------------------------------------------------

def dump_yaml(data, key_order=None):
    """Serialize a flat dict (with optional list values) to YAML."""
    lines = []
    keys = key_order if key_order else list(data.keys())
    # Append any keys not in key_order
    if key_order:
        for k in data.keys():
            if k not in keys:
                keys.append(k)
    for k in keys:
        if k not in data:
            continue
        v = data[k]
        lines.append(_dump_kv(k, v))
    return '\n'.join(lines) + '\n'


def _dump_kv(key, val):
    if val is None:
        return f"{key}: null"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    if isinstance(val, int):
        return f"{key}: {val}"
    if isinstance(val, dict):
        lines = [f"{key}:"]
        for k, v in val.items():
            lines.append(f"  {k}: {_dump_inline(v)}")
        return '\n'.join(lines)
    if isinstance(val, list):
        if not val:
            return f"{key}: []"
        # Always inline — our lists are short (skills, blocked_by)
        items = ', '.join(_dump_inline(x) for x in val)
        return f"{key}: [{items}]"
    return f"{key}: {_dump_scalar(str(val))}"


_NEEDS_QUOTE = set(':#[]{},\'"\n&*!|>%@`')


def _dump_scalar(s):
    if s == '':
        return '""'
    if '\n' in s or '\r' in s:
        # A raw embedded newline breaks the line-oriented parse_yaml above no
        # matter how it's quoted (this hand-rolled parser has no block-scalar
        # support), so a value like a multi-line needs-director reason would
        # get written as a literal newline inside a quoted scalar and corrupt
        # the whole file the moment it's read back (t009: took down
        # `plan.sh status` and dispatch for the entire mission). Collapse
        # line breaks to keep every frontmatter value on one line — callers
        # that want to preserve the full text should put it in the task body
        # instead (see cmd_needs_director's use of split_long_freeform).
        #
        # Drop trailing newline(s) first: values captured from a bash command
        # substitution routinely carry one, and collapsing it along with any
        # embedded ones would otherwise leave a dangling " / " at the end
        # (t013 P3 fix — e.g. "QA FAIL: xxx\n" became "QA FAIL: xxx / ").
        s = re.sub(r'(?:\r\n|\r|\n)+$', '', s)
        s = re.sub(r'\r\n|\r|\n', ' / ', s)
    if any(ch in _NEEDS_QUOTE for ch in s):
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if s.lower() in ('true', 'false', 'null', 'yes', 'no', '~'):
        return f'"{s}"'
    if re.fullmatch(r'-?\d+', s):
        return f'"{s}"'
    return s


def _dump_inline(val):
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, int):
        return str(val)
    # Strings: delegate to _dump_scalar so reserved-word / int-shaped strings
    # ('true', '123', etc.) are preserved through the YAML round-trip.
    return _dump_scalar(str(val))


# ---------------------------------------------------------------------------
# Frontmatter helpers (.md task files)
# ---------------------------------------------------------------------------

TASK_META_KEY_ORDER = [
    'id', 'title', 'skills', 'priority', 'status',
    'blocked_by', 'released_deps', 'timeout', 'target_dir', 'worker', 'started_at', 'completed_at',
    'handoff_path', 'fail_head', 'fail_head_waiver', 'pr_number',
    'acceptance_criteria', 'verification', 'rework_count', 'max_rework',
    'qa_checkpoints', 'required_evidence', 'needs_director_reason',
]

TASK_META_DEFAULTS = {
    'acceptance_criteria': None,
    'verification': None,
    'rework_count': 0,
    'max_rework': 3,
}


def serialize_frontmatter(meta, body):
    yaml_text = dump_yaml(meta, key_order=TASK_META_KEY_ORDER)
    if not body.endswith('\n'):
        body = body + '\n'
    return f"---\n{yaml_text}---\n\n{body}"


def parse_task_body(body):
    """Extract Description and Result sections from task body.

    Only the *first* `## Description` and `## Result` headers are treated as
    section boundaries. Later occurrences are kept verbatim inside the current
    section, so a worker-supplied result that contains the literal text
    `## Result` does not silently corrupt the file on the next round-trip.
    """
    sections = {}
    current = None
    buf = []
    for line in body.splitlines():
        m = re.match(r'^##\s+(Description|Result)\s*$', line, re.IGNORECASE)
        if m and m.group(1).lower() not in sections and current != m.group(1).lower():
            name = m.group(1).lower()
            if current is not None:
                sections[current] = '\n'.join(buf).rstrip()
            current = name
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = '\n'.join(buf).rstrip()
    return sections.get('description', '').strip(), sections.get('result', '').strip()


def build_task_body(description, result):
    desc = (description or '').rstrip()
    res = (result or '').rstrip()
    return f"## Description\n{desc}\n\n## Result\n{res}\n"


def extract_trailing_body_section(body):
    """Return any appendix that follows the Result section under its own
    `## <heading>` (other than a literal Description/Result reappearing,
    which parse_task_body already treats as part of Result — see its
    docstring), or '' if there is none.

    cmd_needs_director appends a `## Needs-Director 詳細` appendix to the body
    to preserve a long/multi-line reason (split_long_freeform). But
    parse_task_body has no notion of a third section: everything after the
    first `## Result` header — including that appendix — is captured as one
    opaque `result` string. cmd_done/cmd_fail then discard that entire string
    (`desc, _ = parse_task_body(body)`) and rebuild the body from just
    `description` and the new CLI-supplied result, silently destroying the
    appendix along with it. This is not a rare case — it's the normal
    `needs_director → update --reset → re-run → done` flow (t013 P2 fix).

    Callers that are about to discard the old body via build_task_body should
    call this first and re-append the result (if any) to the new body, so the
    appendix survives independently of whatever the new Result text is.
    """
    lines = body.splitlines()
    result_seen = False
    trailing_start = None
    for i, line in enumerate(lines):
        if re.match(r'^##\s+(Description|Result)\s*$', line, re.IGNORECASE):
            if re.match(r'^##\s+Result\s*$', line, re.IGNORECASE):
                result_seen = True
            continue
        if result_seen and re.match(r'^##\s+\S', line):
            trailing_start = i
            break
    if trailing_start is None:
        return ''
    return '\n'.join(lines[trailing_start:]).strip()


def append_trailing_body_section(body, trailing):
    """Re-append a trailing appendix (from extract_trailing_body_section)
    onto a freshly rebuilt body, if there is one."""
    if not trailing:
        return body
    return body.rstrip() + '\n\n' + trailing + '\n'


FREEFORM_SUMMARY_LIMIT = 200


def split_long_freeform(text, limit=FREEFORM_SUMMARY_LIMIT):
    """Split free-form text into (frontmatter_summary, full_text_or_None).

    frontmatter values must be single-line (see _dump_scalar) and short —
    dumping a long or multi-line string straight into frontmatter is exactly
    what broke t009 (a multi-line needs-director reason corrupted the task
    file and froze the whole mission). Callers that accept free-form text
    from the user (needs-director reason, etc.) should route it through this
    helper: write the returned summary to frontmatter, and when full_text is
    not None, append it to the task body instead so nothing is lost.
    """
    text = text or ''
    has_newline = '\n' in text or '\r' in text
    if not has_newline and len(text) <= limit:
        return text, None
    normalized = re.sub(r'\r\n|\r|\n', ' / ', text).strip()
    if len(normalized) <= limit:
        # Short multi-line input: the ' / '-joined summary already carries
        # everything losslessly, so appending a "full text in body" marker
        # (and duplicating the text into the body) would be misleading and
        # redundant (t013 P3 fix — e.g. "a\nb" used to become "a / b…(全文は
        # 本文を参照)" even though nothing was actually lost).
        return normalized, None
    summary = normalized[:limit].rstrip() + '…(全文は本文を参照)'
    return summary, text


# ---------------------------------------------------------------------------
# State / mission / task I/O
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {'active_missions': [], 'default_mission': None}
    with open(STATE_FILE) as f:
        text = f.read()
    try:
        data = parse_yaml(text, source=STATE_FILE)
    except ValueError as e:
        die(f"failed to parse {STATE_FILE}: {e}")
    if 'active_missions' not in data or data['active_missions'] is None:
        data['active_missions'] = []
    if 'default_mission' not in data:
        data['default_mission'] = None
    return data


def _atomic_write(path, text):
    """Write text to path via tmp + os.replace, with fsync, so a crash mid-write
    cannot leave a half-written file behind. The caller is responsible for
    holding any necessary lock."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; never mask the original exception.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(state):
    out = {
        'active_missions': state.get('active_missions', []) or [],
        'default_mission': state.get('default_mission'),
    }
    # active_missions as block list for readability
    lines = []
    if out['active_missions']:
        lines.append('active_missions:')
        for slug in out['active_missions']:
            lines.append(f"  - {_dump_inline(slug)}")
    else:
        lines.append('active_missions: []')
    lines.append(_dump_kv('default_mission', out['default_mission']))
    _atomic_write(STATE_FILE, '\n'.join(lines) + '\n')


def mission_dir(slug):
    return os.path.join(MISSIONS_DIR, slug)


def mission_yaml_path(slug):
    return os.path.join(mission_dir(slug), 'mission.yaml')


def tasks_dir(slug):
    return os.path.join(mission_dir(slug), 'tasks')


def task_path(slug, task_id):
    return os.path.join(tasks_dir(slug), f"{task_id}.md")


MISSION_KEY_ORDER = ['title', 'slug', 'status', 'created_at', 'completed_at', 'next_task_id', 'max_review_cycles', 'review']


def try_read_queue_file(path, newline=None):
    """`(text, problem)` を返す。読めたら `problem` は None。

    **例外にしないのは、表示系の呼び出し元があるから**である。mission を並べて
    いる途中で 1 つ落ちると、健全な mission まで画面から消える (「1 枚の事故で
    全体を止めない」は、この repo が t009 以来ずっと同じ向きに倒している)。

    `read_queue_file()` は、これに `die()` を足しただけのもの。判定を 2 箇所に
    書かないための分け方で、**種類を確かめる規則そのものは 1 つ**である。
    """
    try:
        return _TASK_CARDS.read_regular_text(path, newline=newline), None
    except _TASK_CARDS.NotARegularFile as e:
        return None, (
            f"{path} is not a regular file ({e})\n"
            f"  hint: queue のファイルは通常ファイルだけです。"
            f"`ls -l {path}` で種類を確かめ、置き違えたものなら削除してください。")
    except OSError as e:
        return None, f"failed to read {path}: {e}"
    except UnicodeError as e:
        # `UnicodeDecodeError` は `OSError` ではなく `ValueError` の側にいるので
        # 上の except では捕まらない。t015 で `read_task_card()` について直した
        # のとまったく同じ漏れが、t017 で新しく作ったこちらに再現していた
        # (Codex 9 巡目 P2) —— 1 つの読めない mission.yaml が、隔離されずに
        # `status` の一覧そのものを中断させる。
        return None, (
            f"failed to decode {path}: {e}\n"
            f"  hint: queue のファイルは UTF-8 です。`file {path}` で確かめ、"
            f"必要なら `iconv -f <元の文字コード> -t utf-8` で書き直すこと。")
    except Exception as e:      # noqa: BLE001 — backstop。
        # 名前の分かっている失敗は上で個別に扱い (そのほうが直し方を書ける)、
        # **残り全部をここで受ける**。「今回の 1 件を足す」形は、次に読み取り
        # 経路へ新しい失敗が入った日に同じ止まり方をもう一度出す。
        return None, f"unexpected failure while reading {path}: {type(e).__name__}: {e}"


def read_queue_file(path, what):
    """queue のファイルを、**種類を確かめてから** 読む。読めなければ `die()`。

    固定パスで開く読み取りにも `lib_task_cards` のガードを当てる (Codex 8 巡目
    P2)。t016 で入れた判定は `tasks/` を *列挙して* 読む経路にしか無く、
    ここ (`load_task` / `load_mission`) は素の `open()` のままだった。

    列挙するかどうかは害の大きさを変えない。これらは `with_lock()` の内側で
    呼ばれるので、書き手のいない FIFO 1 枚で **キューロックを握ったまま**
    止まる —— 止まるのはその mission ではなく、`plan.sh` 全体である。

    待ち時間に上限を付けるのではなく種類で弾くのは、待てば読めるものが 1 つも
    無いから。queue のファイルは通常ファイルしかありえない。

    ※ `load_state()` には **同じ判定を入れていない**。明示的な取引で、理由は
      `knowledge/empty-vs-unobservable.md` §4 にある (そこが
      `tests/test_retirement.py` の回帰テストを成立させている唯一の停止点)。
    """
    text, problem = try_read_queue_file(path)
    if problem is not None:
        die(f"{what}: {problem}")
    return text


def load_mission(slug):
    path = mission_yaml_path(slug)
    if not os.path.exists(path):
        die(f"mission '{slug}' not found at {path}")
    text = read_queue_file(path, 'mission file')
    try:
        return parse_yaml(text, source=path)
    except ValueError as e:
        die(f"failed to parse {path}: {e}")


def save_mission(slug, data):
    os.makedirs(mission_dir(slug), exist_ok=True)
    _atomic_write(mission_yaml_path(slug), dump_yaml(data, key_order=MISSION_KEY_ORDER))


def load_task(slug, task_id):
    path = task_path(slug, task_id)
    if not os.path.exists(path):
        die(f"task '{task_id}' not found in mission '{slug}'")
    text = read_queue_file(path, 'task card')
    try:
        return parse_frontmatter(text, source=path)
    except ValueError as e:
        die(f"failed to parse {path}: {e}")


def save_task(slug, task_id, meta, body):
    os.makedirs(tasks_dir(slug), exist_ok=True)
    _atomic_write(task_path(slug, task_id), serialize_frontmatter(meta, body))


def list_tasks(slug, base_dir=None, quiet=False):
    """Return list of (meta, body) sorted by tNNN.

    読み取りの規則そのものは `scripts/lib_task_cards.py` にある —— parser、
    「識別子はファイル名であって frontmatter の `id` 欄ではない」、信用できない
    カードを `[破損]` として保留する形の 3 点セットで、dispatcher.sh も同じ
    モジュールを読む。ここはそれを mission の slug で呼ぶだけの薄い層である。

    別々のコードで同じ queue を読んでいたのが Codex 5 巡目 P2 の指摘で、そのとき
    ズレは「`plan.sh` は受理して ready と表示するカードで、dispatch サイクルが
    `KeyError` を出して全 mission の割り当てが止まる」という形で出た。

    quiet=True は「同じ実行の中で 2 回目以降に読む」呼び出し用。破損した task に
    ついての hint 付き警告は 1 回出れば十分で、コマンド本体と task-graph の生成が
    同じ警告を二重に出すと、読む側は 2 件壊れていると誤読する。
    """
    tdir = base_dir if base_dir else tasks_dir(slug)
    return _TASK_CARDS.list_task_cards(tdir, warn=None if quiet else _warn_task_card)


def _warn_task_card(msg):
    """lib_task_cards からの 1 件の警告を plan.sh の顔で出す。"""
    print(f"[plan.sh warn] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Dependency readiness — 「依存が満たされた」の唯一の定義
# ---------------------------------------------------------------------------
#
# 規則の本体は scripts/lib_dep_rules.py にある。pull / task-graph / dispatcher
# の 3 者が同じ 1 つの定義を読むためで、経緯と「なぜフォールバックを置かない
# のか」はそのファイルの docstring に書いてある。
#
# 読み込みは `_load_scripts_module()` 経由 (どこから読むか・なぜ try で包まない
# かは、その定義の上のコメントにまとめてある)。

_DEP_RULES = _load_scripts_module('lib_dep_rules')
DEAD_DEP_STATUSES = _DEP_RULES.DEAD_DEP_STATUSES
HELD_DEP_STATUSES = _DEP_RULES.HELD_DEP_STATUSES
unmet_dependencies = _DEP_RULES.unmet_dependencies
card_dependencies = _DEP_RULES.card_dependencies
declared_dependencies = _DEP_RULES.declared_dependencies


def held_dependency_hint(task_id, held, slug):
    """保留 (failed の依存) を見た人が、次に何を打てばよいか分かる 1 行。

    保留が「永久保留」という別の outage にならないための出口の案内。status
    (要約 / 詳細) と pull の診断が同じ文面を使う (別々に書くと、解除コマンドの
    綴りが片方だけ古くなる)。

    **`slug` は必須 (既定値なし)**。task ID は mission ごとの自動採番なので別 mission
    に同じ `tNNN` が普通にあり、`--mission` の無いコマンドは default_mission の
    task を解除する / skip する。出した文面をそのまま打つと別の task が解除され、
    意図した task は保留のまま —— 出口の案内が罠になる (Codex PR #217 P1)。
    呼び忘れを黙って許す既定値を置かず、渡し忘れた経路は呼んだ瞬間に落ちる。
    """
    return (
        f"HELD: 依存 {', '.join(held)} が failed — Director の判断待ち。"
        f"進めるなら `plan.sh release-dep {task_id} --mission {slug}` "
        f"(fix が要るなら task を足す / "
        f"中止なら `plan.sh update {task_id} --mission {slug} --status skipped`)"
    )


# ---------------------------------------------------------------------------
# herdr-task-graph 連携（任意の付加機能）
# ---------------------------------------------------------------------------
#
# active mission の全 task を herdr plugin `herdr-task-graph` の入力形式で
# 書き出す。crewvia はこの plugin が無くても完全に動く — 生成の失敗も、生成を
# 止めたことも、plan.sh の終了コードや動作を一切変えてはいけない。
#
# 生成を呼ぶのは queue を書き換えるサブコマンドの **後** (キューロックの外)。
# 常駐プロセスは増やさない。dispatcher のサイクルにも載せない — dispatcher が
# 死んでいる間こそ「今どうなっているか」を見たいのに、そこに載せると更新が
# 止まるため。

#: queue を書き換えるサブコマンド = 生成を呼ぶ経路。正は冒頭の usage 行と
#: 末尾の dispatch テーブル (tests/test_task_graph.py が突き合わせる)。
QUEUE_MUTATING_SUBCOMMANDS = {
    'init', 'add', 'pull', 'done', 'needs-director', 'fail', 'update', 'release-dep',
    'retire',
    'ready-for-verification', 'verify-result', 'review', 'launch', 'archive',
}

#: queue を読むだけのサブコマンド = 生成を呼ばない経路。
#: `task-graph` 自身もここ (queue は書き換えず、生成物だけを書く)。
QUEUE_READONLY_SUBCOMMANDS = {
    'lint', 'status', 'resync', 'dashboard-data', 'task-graph', 'resolve-mission',
}

#: crewvia の status → (plugin の status, title に付ける印)。
#: plugin 側は 6 状態 (done/running/blocked/ready/waiting/failed) しか持たない
#: ので、同じ状態に畳まれるものは印で見分ける。`pending` だけは依存の状態で
#: ready / waiting に分かれるため、ここではなく build_task_graph() が決める。
TASK_GRAPH_STATUS_MAP = {
    'done':                   ('done',    None),
    'verified':               ('done',    None),
    'skipped':                ('done',    '[skip]'),
    'in_progress':            ('running', None),
    'verifying':              ('running', None),
    'failed':                 ('failed',  None),
    'verification_failed':    ('failed',  '[検証NG]'),
    # `cancelled` は DEAD_DEP_STATUSES の側 —— 「もう完了しない」が確定した
    # 終端で、`done` / `verified` / `skipped` (= TERMINAL_STATUSES) のような
    # 「完了した」ではない。だから done ではなく failed に畳む: そうすれば
    # 「依存先が終端 → 下流は READY」という見え方が failed とまったく同じに
    # なり、依存規則と矛盾しない。done に畳むと、中止したものを完了したと
    # 主張することになる。失敗ではないことは印で分ける ([skip] と同じやり方)。
    'cancelled':              ('failed',  '[中止]'),
    # (c) blocked_reason 付きで明示的に止められている
    'blocked':                ('blocked', '[停止]'),
    # (b) 人間の判断待ち。(a) 依存待ち (= waiting) とは plugin の状態そのもので
    #     分かれ、(c) とは印で分かれる。
    'needs_director':         ('blocked', '[要判断]'),
    'needs_human_review':     ('blocked', '[要判断]'),
    'ready_for_verification': ('blocked', '[要判断]'),
    # パース失敗。title は list_tasks が既に `[破損] ...` にしている。
    CORRUPT_TASK_STATUS:      ('failed',  None),
}

#: 表に無い status。done にも ready にも倒さない — 「進んでよい」と読める側に
#: 倒すと、知らない状態が黙って実行可能に見える。
TASK_GRAPH_UNKNOWN = ('blocked', '[status不明]')

#: ペインを持たない実行者に回る skill。`pane_match` は「この task が今どのペイン
#: で動いているか」を plugin に教える欄なので、ペインが存在しない実行者に対して
#: 名前を書くのは、当たらないだけでなく事実として誤りになる。
#:
#: `codex-review` は dispatcher が kai-review.sh を `nohup` + detached process
#: group で起動する (dispatcher.sh CODEX_REVIEW_SKILLS)。mux のペインは作られ
#: ないので、Kai-codex というペインはどこにも無い。QA t002 の指摘 F-1c は
#: `Kai-codex-worker` と書かれていたことだが、正しい名前に直すのではなく欄ごと
#: 出さないのが正解 —— 正しい名前に直しても、当たらないことは変わらない。
PANELESS_SKILLS = {'codex-review'}

#: pane_match を出してよい status (allowlist)。「この card にまだ Worker が
#: 就いている」と plan.sh 自身が言える状態だけを並べる。
#:
#: 除外側を数える書き方 (`TERMINAL_STATUSES に無ければ出す`) では足りない。
#: `cmd_fail` は **完了を記録しつつ `worker` 欄を残す** ので、`failed` の card は
#: 終わったあとも Worker 名を持ち続ける。`cancelled` / `verification_failed` /
#: `corrupted` も同じで、いずれも TERMINAL_STATUSES には入っていない。crewvia は
#: Worker 名を使い回すので、それらに pane_match を出すと **無関係な task の
#: ペイン** を指す。`pending` は `--reset` が worker を消すので通常は空だが、
#: 残っていても「誰も就いていない」が正しい。
#:
#: 知らない status は出さない側に倒れる (allowlist なので既定が除外)。
TASK_GRAPH_PANE_STATUSES = {
    'in_progress',            # pull が assignment を公開した直後の状態
    'verifying',              # 検証中 — card はまだ Worker のもの
    'ready_for_verification', # 検証待ち — assignment は撤去されない
    'needs_director',         # 判断待ち — assignment は撤去されるが card の worker は残る (t001)
    'needs_human_review',     # 判断待ち — assignment は撤去されない
}


def task_graph_enabled():
    """停止スイッチ。`CREWVIA_TASK_GRAPH=0` で生成を完全に止める。

    生成は plan.sh の queue 変更経路すべてに乗り、全 Worker と両デーモンが叩く。
    「重い・壊れた」が分かったときに revert PR しか道が無い状態にしないための
    退避路であって、plugin の有無とは別の話。
    """
    return os.environ.get('CREWVIA_TASK_GRAPH', '1').strip().lower() not in (
        '0', 'false', 'off', 'no',
    )

#: assignment が無くても pane_match を出してよい status (`task_graph_assignment_holds`)。
#: `plan.sh needs-director` が assignment を外すので、判断待ちはここに入る。
TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT = {'needs_director'}


def task_graph_repo_root():
    """生成物を置くリポジトリ。`CREWVIA_REPO_ROOT` を優先する。

    REPO_ROOT はスクリプトの位置 (`dirname $0/..`) で決まるので、Worker が
    worktree 側の plan.sh を叩くと worktree の registry を指す。そこに書くと、
    Director が開いているファイルは Worker の pull / done では一切更新されない
    — このミッションの中心価値だけが、隔離テストには映らない形で失われる。
    plan.sh は既に retirement_reservation() と cmd_done の bump-task-count で
    同じ優先順を使っている。
    """
    return os.environ.get('CREWVIA_REPO_ROOT') or REPO_ROOT


def task_graph_path():
    """生成物のパス。`CREWVIA_TASK_GRAPH_FILE` で上書きできる。

    既定は `<root>/registry/task-graph/tasks.json` — crewvia 側を正とし、plugin
    の config dir からここへ symlink させる (`HERDR_TASKS_FILE` は稼働中の
    herdr のペインに届かない: knowledge/task-graph.md)。plugin の config dir に直接書く
    案は採らない: 書き先が plugin の内部レイアウトに依存し、plugin が無い環境や
    別バージョンで壊れる。crewvia の中に置けば、plugin が無くても
    `plan.sh task-graph` の出力として意味を持つ。
    """
    explicit = os.environ.get('CREWVIA_TASK_GRAPH_FILE', '').strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(task_graph_repo_root(), 'registry', 'task-graph', 'tasks.json')


def task_graph_queue_matches_root():
    """今いじっている queue が、生成物を置くリポジトリの queue かどうか。

    本番では start.sh が `CREWVIA_QUEUE=$CREWVIA_REPO_ROOT/queue` を必ず export
    するので、この条件は常に成り立つ。成り立たないのは `CREWVIA_QUEUE` だけを
    別の場所に向けた実行 (隔離テストなど) で、そのとき本体の registry を
    上書きすると Director が開いているファイルにテスト用の queue が映る。
    書き先を明示された場合 (`CREWVIA_TASK_GRAPH_FILE`) は呼び出し側の意図が
    はっきりしているので、この判定は挟まない。
    """
    if os.environ.get('CREWVIA_TASK_GRAPH_FILE', '').strip():
        return True
    try:
        expected = os.path.join(task_graph_repo_root(), 'queue')
        return os.path.realpath(QUEUE_DIR) == os.path.realpath(expected)
    except OSError:
        return False


def _task_graph_worker(meta):
    """task の `worker` 欄を Worker 名として読む。無ければ None。

    `plan.sh update --worker null` が `worker: "null"` (引用符付きの文字列) を
    書く既知の事故があるので、文字列としての null も不在として扱う。
    """
    worker = meta.get('worker')
    if not isinstance(worker, str):
        return None
    worker = worker.strip()
    if not worker or worker.lower() in ('null', 'none', '~'):
        return None
    return worker


def task_graph_assignment_holds(worker, slug, task_id, status=None):
    """`queue/assignments/<worker>` が、いまこの task を指しているか。

    card の `worker` 欄は **履歴** で、crewvia は Worker 名を使い回す。だから
    「いまどのペインに居るか」を card だけから決めると、名前の使い回しの分だけ
    必ず誤る。`queue/assignments/<agent>` は crewvia が「存在 = busy / 不在 =
    idle」を表すために持っている一行の事実なので、pane_match の根拠はそちらに
    置く (status の allowlist との **AND**)。

    撤去を伴う判定ではないので classify_assignment() の世代照合までは要らない。
    ここで問うているのは「この名前の Worker が、いまこの card に就いているか」
    だけで、後任か先任かで pane の宛先は変わらない。

    読めない・無い・別の task を指している — どれも「分からない」ではなく
    **出さない** に倒す。pane_match が無ければ plugin はペインを結び付けない
    だけだが、間違った pane_match は無関係なペインを指す。

    唯一の例外 (t001 / backlog #13): `needs_director` の card は、assignment が
    **本当に無い** (ENOENT) ときも出す。`plan.sh needs-director` が assignment を
    外すので、判断待ちの Worker は常に「無い」— これを弾くと、Director がいちばん
    ペインに飛びたい node だけが実運用で pane_match を失う。代償は、名前を使い回した
    別の Worker が idle で居るとき、その pane を指しうること (別の task に就いて
    いれば assignment が指す先が違うので出ない)。読めない assignment は「無い」では
    ないので、この例外にも入らない。
    """
    if agent_name_problem(worker):
        return False
    path = os.path.join(ASSIGNMENTS_DIR, worker)
    # 素の open() だと、置き違えた FIFO 1 枚で task-graph の生成が返らなく
    # なる (t018)。読めないときは「出さない」に倒す —— 上の docstring の通り。
    text, problem = try_read_queue_file(path)
    if problem is not None:
        return status in TASK_GRAPH_PANE_WITHOUT_ASSIGNMENT and _is_enoent(path)
    return text.strip() == f'{slug}:{task_id}'


def _is_enoent(path):
    """`path` が本当に無いか。`EACCES` など観測できなかった場合は False
    (`exists()` は両者を潰すので使わない)。"""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def break_dependency_cycles(nodes):
    """循環を閉じている辺だけを落とし、落とした側の title に印を残す。

    plugin の `load_config` は循環を見つけると `ValueError` を投げ、**ファイル
    全体** を拒否する。つまり 1 つの mission の循環が、他の mission も含めた
    全 DAG を表示不能にする。そして crewvia 側はいま循環を作れてしまう:
    `plan.sh lint` は FAIL にするが `plan.sh update --blocked-by` は rc=0 で
    通すので、普通の操作で可視化が全滅しうる。

    採ったやり方は、list_tasks が破損 task を `[破損]` 疑似ステータスで隔離する
    のと同じ —— **壊れているところだけを隔離して、残りは今までどおり見せる**。
    task は 1 つも消さず、落とすのは循環を閉じている辺だけにする。

    落とす辺は DFS の後退辺 (いま辿っている経路上の node に戻る辺) で決める。
    後退辺だけが循環を閉じるので、これを外せば必ず非循環になり、外す数も最小で
    済む。自己依存 (t001 → t001) も後退辺として同じ経路で落ちる。走査順は
    `nodes` の順 (= list_tasks の順) に固定なので、同じ入力なら必ず同じ結果。

    status は触らない。循環している task は実際に pull できない (依存が永久に
    満たされない) ので、辺を落とす前に決まった `waiting` が事実のまま正しい。
    落とした辺は `[循環依存: <id>]` として title に出す —— dangling を
    `[依存不明: ...]` で見せるのと同じで、消した情報を黙って消さないため。

    全 mission の node をまとめて渡してよい。`depends_on` は同じ slug で修飾されて
    いるので辺は mission をまたがず、どの mission の DFS も他の mission に入って
    いけない。つまり 1 回の走査が mission ごとの走査そのものであり、「壊れた
    mission だけを隔離する」はこの形でも変わらず成り立つ。
    """
    by_id = {n['id']: n for n in nodes}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node_id: WHITE for node_id in by_id}
    cut = {}

    for root in [n['id'] for n in nodes]:
        if color[root] != WHITE:
            continue
        color[root] = GREY
        stack = [(root, iter(list(by_id[root]['depends_on'])))]
        while stack:
            node_id, deps = stack[-1]
            descended = False
            for dep in deps:
                if dep not in by_id:
                    continue
                if color[dep] == GREY:
                    # 後退辺 — この辺が循環を閉じている。
                    cut.setdefault(node_id, set()).add(dep)
                elif color[dep] == WHITE:
                    color[dep] = GREY
                    stack.append((dep, iter(list(by_id[dep]['depends_on']))))
                    descended = True
                    break
            if not descended:
                color[node_id] = BLACK
                stack.pop()

    for node_id, dropped in cut.items():
        node = by_id[node_id]
        node['depends_on'] = [d for d in node['depends_on'] if d not in dropped]
        node['title'] = (
            '[循環依存: ' + ', '.join(sorted(dropped)) + '] ' + node['title']
        )
    return nodes


#: task が 1 件も無いときに置くプレースホルダの id。`<slug>:<tNNN>` 形式の実 id
#: とは別物にしてあるが、そもそも「他に node が 1 つも無い」ときにしか置かない
#: ので、id が衝突することは構造上ありえない。
TASK_GRAPH_EMPTY_ID = 'crewvia:no-active-tasks'


def task_graph_placeholder(slugs):
    """task が 0 件のときに置く、node 1 件だけのグラフの中身を返す。

    plugin は `tasks` が空だと `ValueError: tasks must be a non-empty array` で
    ファイル全体を拒否する。そして task 0 件は異常ではない —— **最後の mission
    を archive した直後、つまりミッションとミッションの間の普通の状態** がまさに
    それで、起動なら例外、稼働中なら `r` (reload) がエラー表示になる。通常運用で
    必ず通る状態なので、ここで壊れるのは許容できない。

    採れた道は 3 つあった。(1) ファイルを書かない、(2) 前回の内容を残す、
    (3) プレースホルダを 1 件置く。

    (1) と (2) は言い方が違うだけで、ディスク上の結果は同じ —— 画面には
    *もう存在しない* mission の DAG が、現在の姿として出たままになる。グラフは
    「今どうなっているか」を見るためのものなので、古い姿を現在として見せるのは、
    何も見せないより悪い。しかも直前が最後の mission の完了直後なら、全部 done
    の画面が次のミッションが始まるまで延々と残る。

    採ったのは (3)。ファイルは常に妥当で、常に現在を映し、「今は何も無い」が
    まさにそう読める形で出る。status は `blocked`: 実行可能に読める側 (`ready`)
    にも、完了したと読める側 (`done`) にも倒さない —— TASK_GRAPH_UNKNOWN と
    同じ理由で、偽の node が動かせる / 終わっていると見えるほうが害が大きい。
    """
    if not slugs:
        reason = 'active mission がありません'
    else:
        reason = 'active mission に task がありません (' + ', '.join(slugs) + ')'
    return {
        'id': TASK_GRAPH_EMPTY_ID,
        'title': '[表示する task なし] ' + reason,
        'depends_on': [],
        'status': 'blocked',
    }


#: id が使えなかった node に振り直す id の土台。実 id は `<slug>:<tNNN>` なので、
#: この形と衝突することはない。
TASK_GRAPH_UNUSABLE_ID = 'crewvia:id-unusable'


def _free_task_graph_id(base, used):
    """`used` に無い id を `base` から作る。"""
    candidate = base
    n = 1
    while candidate in used:
        n += 1
        candidate = f'{base}#{n}'
    return candidate


def isolate_untrustworthy_ids(nodes):
    """id を理由に plugin がファイル全体を捨てる 2 条件を、node を消さずに潰す。

    plugin の `load_config` は id が空でも重複していても **ファイル全体** を
    捨てる。1 枚のカードの事情で全 mission の DAG が消えるという、t010 の循環・
    空配列とまったく同じ型の巻き添えである。

    既知の発生源 (frontmatter の id の名乗り替え) は list_tasks が塞いだので、
    ここはその後ろに立つ最後の関門である。**今はここより手前で潰れている** —
    それでも置くのは、node を作る経路が増えたときに、増やした側が気付かないまま
    ファイル全体を落とせてしまう形を残さないため。ゲートを通る限り publish 物が
    契約を満たす、と言い切れることに意味がある。

    潰し方は隔離であって削除ではない。node は消さず、id を衝突しない形に振り直し、
    何が起きたかを title に残す。黙って落とすと、その task が DAG から消えた理由
    が誰にも分からなくなる。status は `blocked` に倒す —— どのカードなのか言えない
    node が `ready` (動ける) や `done` (終わった) に見えるほうが害が大きい。
    """
    used = {n.get('id') for n in nodes
            if isinstance(n.get('id'), str) and n.get('id')}
    seen = set()
    for node in nodes:
        node_id = node.get('id')
        if not isinstance(node_id, str) or not node_id:
            mark = '[id不正]'
            node_id = _free_task_graph_id(TASK_GRAPH_UNUSABLE_ID, used)
        elif node_id in seen:
            mark = f'[id重複: {node_id}]'
            node_id = _free_task_graph_id(node_id, used)
        else:
            seen.add(node_id)
            continue
        node['id'] = node_id
        node['title'] = mark + ' ' + str(node.get('title') or '')
        node['status'] = 'blocked'
        used.add(node_id)
        seen.add(node_id)
    return nodes


def drop_unresolvable_dependencies(nodes):
    """publish する node のどれも指していない辺を落とし、印を title に残す。

    plugin は解決できない `depends_on` でもファイル全体を捨てる。落とす理由は
    それだけで、落とした事実は隠さない —— 黙って消すと、依存が最初から無かった
    ように見える。status は触らない: 不明な依存は `unmet_dependencies()` の側で
    すでに「満たされていない」と数えられており、waiting のままが事実である。

    印に出す id は、同じ mission の中の依存なら `<slug>:` を外して見せる。
    mission をまたぐ依存だけが修飾付きで出るので、どちらなのかが一目で分かる。
    """
    known = {n['id'] for n in nodes}
    for node in nodes:
        deps = node.get('depends_on') or []
        missing = sorted({d for d in deps if d not in known})
        if not missing:
            continue
        node['depends_on'] = [d for d in deps if d in known]
        own = node['id'].rsplit(':', 1)[0] + ':'
        shown = [d[len(own):] if d.startswith(own) else d for d in missing]
        node['title'] = (
            '[依存不明: ' + ', '.join(shown) + '] ' + str(node.get('title') or '')
        )
    return nodes


def enforce_task_graph_contract(nodes, slugs):
    """publish の直前に立つ唯一のゲート。

    plugin の `load_config` は 4 つの理由で **ファイル全体** を捨てる —— 空の
    tasks、id が空 / 重複、解決できない `depends_on`、依存の循環。どれも 1 つの
    mission (ときに 1 枚のカード) の事情で起きるのに、巻き添えになるのは全
    mission の DAG である。

    **4 つの対処をここに集める理由**: t010 で 2 つ、Codex 4 巡目で 1 つと、理由は
    増え続けている。潰し方が別々の場所に書かれていると、次の 1 件が来たときに
    片方だけ直して穴が開く —— このミッションとその前のミッションで繰り返し起きた
    型そのものである。「ここを通った node は plugin が受け取れる」とゲート 1 つで
    言い切れる形にしておけば、次の 1 件もここに足すしかなくなる。

    順番には意味がある。

    1. **id** が先。以降の処理はどれも `{n['id']: n}` の形で node を引くので、
       重複が残っていると片方が黙って消える。
    2. **解決できない辺** を落としてから、
    3. **循環** を切る。循環の判定は辺が全部解決している前提で書いてある。
       切るのは後退辺だけなので、node は 1 つも減らない。
    4. **空** は最後。1〜3 は node を減らさないので、空になりうるのは入力が
       最初から空だったときだけである。
    """
    nodes = isolate_untrustworthy_ids(nodes)
    nodes = drop_unresolvable_dependencies(nodes)
    nodes = break_dependency_cycles(nodes)
    if not nodes:
        nodes = [task_graph_placeholder(slugs)]
    return nodes


_LIB_MUX = None


def task_graph_pane_id(worker):
    """Worker のペインの `pane_id`。確かに言えるときだけ返し、他は None。

    根拠は `registry/mux/<Worker>-worker.json` (start.sh が Worker 起動時に
    書く spawn 記録)。**herdr には触れない** — 記録と /proc を読むだけで、
    `lib_mux.recorded_herdr_pane_id()` が「記録の server がまだ生きているか」
    (= その pane id が今の server の世代のものか) まで見る。ロックも取らない
    (`.records.lock` を待つと、`retire --no-wait` 経由で watchdog が同期で
    待たされる)。書き手は in-place の書き込みなので半端な読み取りはあるが、
    JSON として読めなければ None になるだけ。

    None のとき呼び出し側は `pane_id` を書かず、従来の `pane_match` だけが残る。
    **どんな失敗でも生成全体を落とさない** — ペインへ飛べることは付加機能で、
    図が描けることのほうが上。
    """
    global _LIB_MUX
    try:
        if _LIB_MUX is None:
            _LIB_MUX = _load_scripts_module('lib_mux')
        return _LIB_MUX.recorded_herdr_pane_id(
            f'{worker}-worker', repo_root=task_graph_repo_root())
    except Exception as e:      # noqa: BLE001 — 上の docstring の通り。
        print(f'[plan.sh] task-graph: {worker} の pane_id を解決できない '
              f'({type(e).__name__}: {e}) — pane_match だけを書く', file=sys.stderr)
        return None


def build_task_graph(state):
    """active mission 全部を plugin の入力形式に変換する。

    ここは翻訳だけを行う。plugin が受け取れる形にする責任は
    `enforce_task_graph_contract()` が 1 つで持つ。

    id は `<slug>:<tNNN>` に修飾する。mission をまたぐと t001 が衝突するため。
    `depends_on` も同じ修飾で解決する (blocked_by は mission 内の id)。
    """
    # 同じ slug が 2 回並んでいても mission は 1 つ。state.yaml は復旧手順で手を
    # 入れるファイルなので、2 行になること自体は起こる。落とさずに素通しすると
    # **全 node が 2 つずつ** 出て、ファイルが丸ごと読めなくなる。
    slugs = []
    for s in (state.get('active_missions') or []):
        if s and s not in slugs:
            slugs.append(s)
    nodes = []
    for slug in slugs:
        if not os.path.isdir(mission_dir(slug)):
            continue
        mission_nodes = []
        tasks = list_tasks(slug, quiet=True)
        done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
        task_statuses = {m['id']: m.get('status') for (m, _) in tasks}
        for (meta, _body) in tasks:
            task_id = meta.get('id')
            if not task_id:
                continue
            raw_status = meta.get('status')
            # 1 つも落とさない (`if d` で falsy を捨てると `[null]` が「依存なし」になる)。
            # 形の違う card は読み取りが隔離済みで、ここは 2 枚目の網。
            blocked_by = declared_dependencies(meta.get('blocked_by'))
            if raw_status == 'pending':
                # crewvia 側で READY を導出して明示的に書く。plugin の導出に
                # 委ねると、`failed` の依存を満たされた扱いにする crewvia の
                # 規則が伝わらず、QA FAIL 直後だけ WAIT と表示される。
                verdict = card_dependencies(meta, done_ids, task_statuses)
                if verdict.held:
                    # failed の依存は Director の判断待ち。plugin の 6 状態には
                    # 「判断待ち」が無いので blocked に畳み、印で理由を残す
                    # (needs_director と同じやり方)。waiting のままだと
                    # 「依存が終われば勝手に進む」と読めてしまう。
                    status = 'blocked'
                    marker = f"[保留: {', '.join(verdict.held)} が failed]"
                else:
                    status = 'waiting' if verdict.unmet else 'ready'
                    marker = None
            else:
                status, marker = TASK_GRAPH_STATUS_MAP.get(raw_status, TASK_GRAPH_UNKNOWN)

            title = meta.get('title') or task_id
            if marker:
                title = marker + ' ' + str(title)

            # depends_on は依存が無くても `[]` で必ず出す。省略が許されるかは
            # plugin の schema 次第だが、空リストはどちらの読み方でも通る。
            # 存在しない task への依存をここで落とさないのは、それが plugin の
            # 契約の話であって翻訳の話ではないから —— ゲートが落として印を残す。
            node = {
                'id': f'{slug}:{task_id}',
                # 画面に出す短い名前。plugin が `label` に対応していれば `id`
                # (`<slug>:tNNN`、長くて読めない) の代わりに使う。対応していない
                # 版は未知の欄として無視する。`id` は mission をまたぐ一意性と
                # 依存解決のためにそのまま残す。
                'label': str(task_id),
                # 複数 mission を並べると `label` (`tNNN`) は mission ごとの採番で
                # 重複する。plugin が `group` に対応していれば箱の右端に mission
                # slug の末尾が出て区別できる。対応していない版は未知の欄として
                # 無視する。slug は str なので、plugin の「group は文字列」の検証
                # (満たさないとファイル全体を拒否する) を破らない。
                'group': str(slug),
                'title': str(title),
                'depends_on': [f'{slug}:{d}' for d in blocked_by],
                'status': status,
            }
            worker = _task_graph_worker(meta)
            paneless = bool(set(meta.get('skills') or []) & PANELESS_SKILLS)
            if (worker and not paneless
                    and raw_status in TASK_GRAPH_PANE_STATUSES
                    and task_graph_assignment_holds(worker, slug, task_id, raw_status)):
                # crewvia のペイン名は `<AGENT_NAME>-<ROLE>` (start.sh)。Worker は
                # `<名前>-worker`。終わった task の worker 欄は履歴であって、今
                # そのペインが居る場所ではない (名前は使い回される)。だから
                # 「card がまだ Worker のものだと言っている」(status) と
                # 「公開中の assignment がこの task を指している」(事実) の
                # **両方** が揃ったときにだけ出す。片方でも欠ければ出さない。
                node['pane_match'] = f'{worker}-worker'
                # pane_match は live の herdr の snapshot には当たらない
                # (knowledge/task-graph.md §4-4) ので、spawn 記録の pane_id も
                # 書く。記録が無い・古い・読めないときは書かない (pane_match のまま)。
                pane_id = task_graph_pane_id(worker)
                if pane_id:
                    node['pane_id'] = pane_id
            mission_nodes.append(node)

        nodes.extend(mission_nodes)

    nodes = enforce_task_graph_contract(nodes, slugs)

    if len(slugs) == 1:
        title = f'crewvia / {slugs[0]}'
    else:
        title = f'crewvia / {len(slugs)} missions'
    return {'title': title, 'tasks': nodes}


#: publish の直列化ロックを待つ上限 (秒)。付加機能が本体を止めないための上限で
#: あって、直列化の強さではない。flock はプロセスの死で必ず外れるので、ここに
#: 引っかかるのは「publish の途中で生きたまま止まっている実行」がいるときだけ。
TASK_GRAPH_LOCK_WAIT_SECONDS = 10.0
TASK_GRAPH_LOCK_POLL_SECONDS = 0.02

#: 印を触るあいだだけ取る小さなロックを待つ上限 (秒)。本ロックと同じ理由で
#: **必ず有限** にする。保持時間はファイル 1 つの読み書きと flock の解放だけ
#: なので、ここに引っかかるのは「印の区間の中で生きたまま止まっている実行」が
#: 居るときだけである。
#:
#: 秒数を本ロックより小さく取るのは、`retire --no-wait` が watchdog の監視
#: ループから **同期で** 呼ばれるからである。watchdog は Worker の生死を見る
#: 唯一の主体なので、その 30 秒の subprocess タイムアウトまで持っていかれると、
#: その間 Worker を誰も見ていないことになる。最悪経路は「本ロック 10 秒 →
#: 印のロック → (取れた本ロックで publish) → 印のロック」なので、上限の合計は
#: 10 + 2 × 2 = 14 秒 + 走査 1 回分に収まる。
TASK_GRAPH_PENDING_LOCK_WAIT_SECONDS = 2.0

#: 読み直しを繰り返す上限。**通常運用では到達しない安全弁** であって、
#: 受け渡しの仕組みではない。読み直しが 1 回増えるのは「この publish の読み
#: 取りを *始めたあと* に新しい要求が置かれた」ときだけなので、ここに届くには
#: その並びが 32 回続く必要がある (= ロック待ちを諦めた実行が 32 回、毎回
#: 読み取り窓の中に飛び込んでくる)。届いてしまった場合も要求は **消さずに**
#: 残して手を引き、1 行報告する — 黙って落とす経路はどこにも作らない。
TASK_GRAPH_MAX_ROUNDS = 32


def _task_graph_lock_path(path):
    """生成物ごとの直列化ロック。書き先が違う実行同士は競合しない。"""
    return path + '.lock'


def _task_graph_pending_path(path):
    """「ロックを待ち切れず引き返した実行がいる」ことを表す印。"""
    return path + '.pending'


def _task_graph_pending_lock_path(path):
    """印を触るあいだだけ取る小さなロック。"""
    return path + '.pending.lock'


@contextlib.contextmanager
def task_graph_pending_lock(path, wait_seconds=None):
    """印の読み書きと、本ロックの解放とを、並べ替えさせないための小さなロック。

    守るのは印そのものではなく、印と本ロックの **順序** である。受け渡しが
    成立するかどうかは、次の 1 点だけに懸かっている:

        「印が無いことを確認して本ロックを解放する」(保持者) と
        「印を置く」(要求者) が、互いに割り込めないこと。

    割り込めると、確認と解放のあいだに置かれた印を保持者が見ないまま手を引き、
    要求者はもう誰も待っていないロックを諦めて帰る。その印は次に誰かが queue を
    触るまで誰にも消費されず、**最後の queue 変更が無期限に見えないまま残る**。

    そこで保持者は「確認 → 解放」をこのロックの中でまとめて行い、要求者は
    「印を置く」をこのロックの中で行う。どちらが先にこのロックを取ったかで
    順序が必ず決まるので、上の取りこぼしは構造として起きない。

    保持時間はファイル 1 つの読み書きと flock の解放だけで、**キューロックには
    一切触れない**。要求者はこのロックを持ったまま本ロックを待たない (持った
    ままにすると、本ロックの保持者が確認に入れず互いに待つ) 。

    待ちは有限
    ----------
    上の理屈は「このロックがすぐ空く」ことを前提にしているが、**前提が外れた
    ときの倒し方を持たないと、前提そのものが凶器になる**。短いはずのロックを
    期限なしで待つと、区間の中で生きたまま止まっている実行が 1 つ居るだけで、
    以降の queue 変更コマンドが全て無期限に詰まる。可視化は付加機能なので、
    倒す先は「グラフが少し古くなる」でなければならず、「plan.sh が待たされる」
    であってはならない。

    そこで取得は上限付きにし、**取れたかどうかを yield で返す**。取れなかった
    ときにどう倒すかは呼び出し側が決めるが、どちらの呼び出し側も
    **「印を消さない」に倒す** — 消してよいのは「無い」と確認できたときだけで、
    確認できていない以上、消せば要求者の最後の変更がそのまま落ちる。残せば
    次に queue を触った実行が拾うので、失われるのは即時性だけである。

    受け渡しの不変条件は壊れない。区間に入れた者同士の順序は従来どおり flock が
    決めており、上限を足しても「確認と解放が一区間に入る」ことは変わらない。
    変わるのは「区間に入れなかった者が居りうる」ことだけで、入れなかった者は
    印に一切触れないので、置かれた印が消えることも、消えた印が復活することも
    ない。
    """
    if wait_seconds is None:
        wait_seconds = TASK_GRAPH_PENDING_LOCK_WAIT_SECONDS
    lock_path = _task_graph_pending_lock_path(path)
    os.makedirs(os.path.dirname(lock_path) or '.', exist_ok=True)
    lf = open(lock_path, 'a+')
    acquired = False
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(TASK_GRAPH_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        try:
            if acquired:
                fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()


def acquire_task_graph_lock(path, wait_seconds=None):
    """publish を直列化するロックを取る。待ち切れなければ None。

    `wait_seconds=0` は「1 回だけ試す」。要求を置いたあとに、いま保持者が
    居るのかどうかを確かめるために使う (下の refresh_task_graph を参照)。

    **キューロックとは別物で、キューロックの内側からは決して取らない。**
    生成がキューロックの外であることは構造テストで固定されているので、
    2 つのロックの順序が逆転する経路は存在しない。

    上限付きで待つのは、付加機能が本体の動作を変えないため。flock は
    プロセスが死ねば必ず外れるので、死んだ実行が plan.sh 全体を止めることは
    ないが、生きたまま止まっている実行に Worker が巻き込まれる道は塞ぐ。

    None が意味するのは「待ち切れなかった」だけである。ロックファイルを
    用意できない (書き先が壊れている等) は OSError のまま投げる — 生成の
    失敗として 1 行報告される方の経路であって、混ぜると書き先が壊れている
    ときに「publish が混んでいる」と読める嘘の診断が出る。
    """
    if wait_seconds is None:
        wait_seconds = TASK_GRAPH_LOCK_WAIT_SECONDS
    lock_path = _task_graph_lock_path(path)
    os.makedirs(os.path.dirname(lock_path) or '.', exist_ok=True)
    lf = open(lock_path, 'a+')
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lf
        except OSError:
            pass
        if time.monotonic() >= deadline:
            lf.close()
            return None
        time.sleep(TASK_GRAPH_LOCK_POLL_SECONDS)


def release_task_graph_lock(lf):
    try:
        fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


def refresh_task_graph():
    """生成物を書き出す。読み取りごと直列化し、tmp + os.replace で置き換える。

    原子的な置換が防ぐのは「半端な JSON を読まれること」だけで、「先に queue を
    読んだ実行が後に書く」入れ替わりは防がない。入れ替わると published な
    グラフは古い姿に巻き戻る。そしてそれは一瞬では消えない: 巻き戻された変更が
    *最後の queue 変更* だった場合、次に誰かが queue を触るまで誤った running が
    居座る。「次のコマンドが直す」は、次のコマンドがある場合の話でしかない。

    そこでロックを取ってから queue を読む。後から入った実行は必ず前の実行の
    publish より後の queue を見るので、最後に書かれる姿は、その時点までに
    commit された queue 変更をすべて含む。ロックはこの生成物専用で、
    **キューロックの保持時間は 1 ミリ秒も伸びない**。

    待ち切れなかった実行は 1 バイトも書かず、読み直しの要求だけを置いて引き返す。
    今 publish している側の読み取りは自分の変更より前かもしれないからである。
    その要求は **必ず誰かに拾われる**。受け渡しは次の 2 つの出口しかなく、
    どちらか一方が必ず成立する:

      (a) 要求を置いたあとに本ロックを取れた → **自分で publish する**。
      (b) 取れなかった → そのとき本ロックを保持している実行が居る。その保持者の
          最終確認は必ずこの要求より後に来る (task_graph_pending_lock の不変
          条件) ので、**保持者が読み直す**。

    (b) が言えるのは、保持者が「要求が無いことの確認」と「本ロックの解放」を
    task_graph_pending_lock の中でまとめて行うからである。確認済みで解放前、
    という中途半端な状態を外から観測できないので、要求を置いた時点でまだ本
    ロックを持っている実行は、まだ確認していないことが確定する。

    読み直しが要るかどうかは、**要求が置かれた時刻と、この publish が queue を
    読み始めた時刻** の比較で決める。読み始めるより前に置かれた要求は、その
    要求者の queue 変更 (要求より前に commit 済み) をこの publish が必ず含む
    ので、消して終わってよい。だからラウンドが増えるのは「読み取り窓の中に
    新しい要求が飛び込んだ」ときだけで、増えたラウンドは毎回、最新の queue を
    publish するという必要な仕事をしている。
    """
    path = task_graph_path()
    lock = acquire_task_graph_lock(path)
    if lock is None:
        # 1 バイトも書かず、要求だけを残す。置けたかどうかに関わらず (a) を
        # 試す — 取れたなら自分で publish するのが最も確実な出口である。
        marked = _mark_task_graph_pending(path)
        lock = acquire_task_graph_lock(path, wait_seconds=0.0)
        if lock is None:
            if not marked:
                # (a) も (b) も成立しない唯一の形。印を置けていないので保持者は
                # 読み直さず、この実行の変更は次に queue を触った実行まで映ら
                # ない。**待ち続けるより古いグラフを選ぶ** — 可視化のために
                # plan.sh を止めないことが、この機能の唯一の約束である。
                print(
                    "[plan.sh warn] task-graph: publish を待ち切れず、読み直しの "
                    "要求も置けませんでした — 次の queue 変更まで古い姿が残りえます",
                    file=sys.stderr,
                )
            return path  # (b) 保持者が居る。その保持者が必ず読み直す。

    released = False
    rounds = 0
    try:
        while True:
            rounds += 1
            # 読み取りを *始めた* 時刻。これより古い要求は、この publish に
            # 含まれていることが言える (要求者は queue を書き終えてから
            # ロックを待ち、諦めてから要求を置くため)。
            read_started = time.monotonic_ns()
            graph = build_task_graph(load_state())
            _atomic_write(path, json.dumps(graph, ensure_ascii=False, indent=2) + '\n')
            with task_graph_pending_lock(path) as in_section:
                if not in_section:
                    # 区間に入れなかった。**印には一切触れずに** 本ロックだけ
                    # 返す。ここで本ロックを握ったまま待ち続けると、止まるのは
                    # この 1 コマンドではなく、以降の全ての queue 変更コマンド
                    # になる (全員が本ロックの上限を払ったうえで同じ所に詰まる)。
                    #
                    # 消さないので要求は失われない。publish 自体はこの直前に
                    # 済んでいるので、残るのは「この瞬間より後に置かれたかも
                    # しれない要求の反映が、次の queue 変更まで遅れる」だけ。
                    released = True
                    release_task_graph_lock(lock)
                    print(
                        f"[plan.sh warn] task-graph: 読み直しの要求を確認できな "
                        f"かったので手を引きました "
                        f"({TASK_GRAPH_PENDING_LOCK_WAIT_SECONDS}s 待機) — "
                        f"要求は残してあります (次の queue 変更で反映されます)",
                        file=sys.stderr,
                    )
                    return path
                if not _task_graph_pending_outstanding(path, read_started):
                    # 未処理の要求は無い。消してから、同じ区間の中で解放する。
                    _clear_task_graph_pending(path)
                    released = True
                    release_task_graph_lock(lock)
                    return path
                if rounds >= TASK_GRAPH_MAX_ROUNDS:
                    # 病的な混み方。要求は **消さずに** 残して手を引く
                    # (次に queue を触った実行が拾う) + 1 行報告する。
                    released = True
                    release_task_graph_lock(lock)
                    print(
                        f"[plan.sh warn] task-graph: 読み直しが "
                        f"{TASK_GRAPH_MAX_ROUNDS} 回続いたので手を引きました "
                        f"— 読み直しの要求は残してあります "
                        f"(次の queue 変更で反映されます)",
                        file=sys.stderr,
                    )
                    return path
    finally:
        if not released:
            release_task_graph_lock(lock)


def _mark_task_graph_pending(path):
    """読み直しを頼む要求を置く。**置けたら True。** 置けなくても失敗させない。

    時刻は **単調時計** で書く。realtime だと NTP の巻き戻しで「読み取りより
    前に置かれた」と誤読し、未処理の要求を消してしまう。単調時計は同じ機体の
    全プロセスで同じ基準を持つので、プロセスをまたいだ比較にそのまま使える。

    区間に入れなかったときは **書かずに False を返す**。ロックの外から書くと、
    保持者の「確認 → 消去」のあいだに置いた印がそのまま消され、置けたつもりで
    要求が落ちる — 置かないより悪い。戻り値を見た呼び出し側が、(a) も成立
    しなかったときに 1 行報告する。
    """
    try:
        pending = _task_graph_pending_path(path)
        os.makedirs(os.path.dirname(pending) or '.', exist_ok=True)
        with task_graph_pending_lock(path) as in_section:
            if not in_section:
                return False
            with open(pending, 'w') as f:
                f.write(f"{os.getpid()} {time.monotonic_ns()}\n")
            return True
    except OSError as e:
        print(
            f"[plan.sh warn] task-graph: 読み直しの要求を書けませんでした ({e})",
            file=sys.stderr,
        )
        return False


def _task_graph_pending_outstanding(path, read_started):
    """まだ処理されていない読み直し要求があるか。**保持者が pending lock 内で呼ぶ。**

    要求が無ければ False。要求の時刻が読み取り開始より後なら True (この
    publish に入っていないかもしれない)。

    読めない・形が違う要求は True に倒す — 余分な読み直しが 1 回増えるだけで、
    失うものは何も無い。逆に「読めないから無かったことにする」と、要求者の
    最後の変更が消える。

    未来の時刻 (= 再起動をまたいで残った前の boot の残骸) は、要求として扱うと
    永久に True を返し続けるので、処理済みとして消す側に倒す。
    """
    # 「無い」= 要求なし。それ以外 (読めない) は要求が **あるかもしれない**
    # 側に倒す —— 取りこぼすと誰も読み直さなくなるため。区別は `ENOENT` の
    # 1 点だけで、`os.path.lexists()` は使わない (`EACCES` でも False になり、
    # 「要求なし」= 取りこぼす側へ倒れる)。
    text = _TASK_CARDS.read_regular_text_or_unreadable(
        _task_graph_pending_path(path))
    if _TASK_CARDS.is_missing(text):
        return False
    if _TASK_CARDS.is_unreadable(text):
        return True
    raw = text.split()
    try:
        marked = int(raw[1])
    except (IndexError, ValueError):
        return True
    if marked > time.monotonic_ns():
        return False  # この boot のものではない残骸
    return marked >= read_started


def _clear_task_graph_pending(path):
    try:
        os.unlink(_task_graph_pending_path(path))
    except OSError:
        pass


def maybe_refresh_task_graph(subcommand):
    """queue を書き換えたあとに生成を 1 回だけ呼ぶ。失敗しても何も壊さない。

    **キューロックの外から呼ぶこと。** ロック保持中に全 mission の走査を足すと、
    その分だけ全 Worker の pull が待たされる。
    """
    if subcommand not in QUEUE_MUTATING_SUBCOMMANDS:
        return
    if not task_graph_enabled():
        return  # 1 バイトも書かず、ログも出さない
    if not task_graph_queue_matches_root():
        print(
            f"[plan.sh warn] task-graph: CREWVIA_QUEUE ({QUEUE_DIR}) が "
            f"{task_graph_repo_root()}/queue ではないので生成しません "
            f"(書き先を指定するなら CREWVIA_TASK_GRAPH_FILE)",
            file=sys.stderr,
        )
        return
    try:
        refresh_task_graph()
    except (Exception, SystemExit) as e:
        # 黙って捨てない。ただし本体の終了コードには触らない。
        print(f"[plan.sh warn] task-graph の生成に失敗しました: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

#: 「ロックを取れなかったので 1 バイトも書かなかった」を表す終了コード。
#: 1 (plan.sh 側の異常) とも 3 (前提不成立 = もう何も owed でない) とも区別する。
#: 呼び出し側 — 常駐デーモンの中で待てない経路 — は「次のサイクルで再試行」に
#: 倒せる。3 と混ぜると「もう用は無い」と読まれて後始末の義務が捨てられる。
LOCK_BUSY = 4


def with_lock(callback, nonblocking=False):
    """キューロックの下で callback を実行する。

    nonblocking=True は「待てない呼び出し側」専用の取得方法である。
    watchdog の retirement は監視ループの中から plan.sh を同期で呼ぶので、
    ブロッキング取得だと**混んでいるキュー 1 つが全 Worker の監視を止める**
    (marker 1 件につき subprocess timeout まで)。デーモンにとっては
    「取れなければ次のサイクルで」の方が正しく、待つ価値のある仕事が無い。
    取れなかった場合は exit LOCK_BUSY で返り、1 バイトも書かない。
    """
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except OSError as e:
        die(f"cannot create queue dir {QUEUE_DIR}: {e}")
    try:
        lf = open(LOCK_FILE, 'a+')
    except OSError as e:
        die(
            f"cannot open queue lock {LOCK_FILE}: {e}\n"
            f"  hint: check write permission on {QUEUE_DIR}, or remove a stale lock file."
        )
    try:
        if nonblocking:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                die(
                    f"[plan.sh] queue lock {LOCK_FILE} is held by another process "
                    f"— --no-wait なので待たずに諦めました (何も変更していません)",
                    LOCK_BUSY,
                )
        else:
            fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return callback()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


# ---------------------------------------------------------------------------
# Assignment files — execution identity
# ---------------------------------------------------------------------------
#
# queue/assignments/<agent> は「存在 = busy / 不在 = idle」という一行の事実を
# dispatcher に伝えるファイルである。中身は <mission>:<task> の 1 行で、hooks も
# 同じ 1 行を読んで TASK_ID を復元している。
#
# ただし <mission>:<task> は *実行* を指していない。crewvia は Worker 名を
# ポジションとして使い回すため、同じ card を同じ名前が pull し直すと、後任の
# assignment は先任のものとバイト単位で同一になる。「内容が一致したから消して
# よい」という判定はそこで壊れ、稼働中の後任の assignment を消してしまう。
# assignment を失った Worker は dispatcher から idle に見えるので、これは
# 「動いている Worker を殺す」経路である。
#
# そこで assignment には世代を添えたサイドカー <agent>.identity を並べ、
# 「この実行の assignment か」を内容ではなく実行アイデンティティで判定する。
# サイドカーを別ファイルに分けているのは、本体の 1 行フォーマットを読む既存の
# consumer (hooks/pre-tool-use.sh, hooks/post-tool-use.sh, dispatcher.sh) を
# 壊さないため。いずれも assignments ディレクトリを列挙せず名前で引くだけなので、
# 隣にファイルが増えても影響しない (<agent>.restarting という先例がある)。

ASSIGNMENTS_DIR = os.path.join(QUEUE_DIR, 'assignments')
IDENTITY_SUFFIX = '.identity'

#: 「前提が外れたので 1 バイトも書かなかった」を表す終了コード。
#: 0 (書いた) とも 1 (plan.sh 側の異常 → 呼び出し側はリトライすべき) とも
#: 区別できるようにしてあるので、呼び出し側は保留に倒せる。
PRECONDITION_UNMET = 3

# classify_assignment() の判定結果。撤去してよいのは ASSIGN_MINE だけ。
ASSIGN_MINE = 'mine'                  # この実行が公開した assignment
ASSIGN_ABSENT = 'absent'              # そもそも公開されていない
ASSIGN_OTHER_TASK = 'other_task'      # 別の task を指している
ASSIGN_SUCCESSOR = 'successor'        # 同じ task の別の実行 (後任) のもの
ASSIGN_UNVERIFIABLE = 'unverifiable'  # 世代を読めない (旧形式 / 破損)


#: assignments ディレクトリで別の意味を持つ suffix。Worker 名として使わせない。
RESERVED_AGENT_SUFFIXES = (IDENTITY_SUFFIX, '.restarting', '.tmp')


def agent_name_problem(agent):
    """Worker 名が assignment ファイル名として使えない理由。使えるなら None。

    ここは撤去 (os.remove) の対象パスを組み立てる根拠でもあるので、ディレクトリ
    を抜けられる名前を弾くのは公開側だけでなく撤去側の防御でもある。
    """
    if not agent or '/' in agent or '\0' in agent or agent in ('.', '..') \
            or agent.startswith('.'):
        return "'/' や先頭の '.' を含まない名前にしてください"
    for suffix in RESERVED_AGENT_SUFFIXES:
        if agent.endswith(suffix):
            return f"'{suffix}' で終わる名前は queue/assignments/ で予約済みです"
    return None


def require_valid_agent_name(agent):
    """不正な名前ならここで止める。**書き込みを 1 バイトも始める前に**呼ぶこと。

    公開側 (pull) の検証をロックの中の save_task() より後に置くと、card だけが
    in_progress になって assignment が無い状態で死ぬ — まさにこの PR が潰した
    「割れたトランザクション」を自分で作ることになる。
    """
    problem = agent_name_problem(agent)
    if problem:
        die(f"invalid agent name {agent!r}: {problem}")


def assignment_path(agent):
    require_valid_agent_name(agent)
    return os.path.join(ASSIGNMENTS_DIR, agent)


def assignment_identity_path(agent):
    return assignment_path(agent) + IDENTITY_SUFFIX


def _read_assignment_identity(agent):
    """サイドカーを読む。読めない・形が違うときは None (= 世代不明)。"""
    text, problem = try_read_queue_file(assignment_identity_path(agent))
    if problem is not None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def publish_assignment(agent, mission, task_id, started_at):
    """<agent> の assignment とその実行アイデンティティを公開する。

    呼び出し側はキューロックを保持していること。サイドカーを先に書くのは、
    「存在 = busy」を意味する本体が、世代の分からない状態で一瞬でも観測され
    ないようにするため。逆順にすると、その隙間に来た後始末が世代を証明できず
    判定不能になる。
    """
    os.makedirs(ASSIGNMENTS_DIR, exist_ok=True)
    _atomic_write(assignment_identity_path(agent), json.dumps({
        'mission': mission,
        'task': task_id,
        'worker': agent,
        'started_at': started_at,
    }, ensure_ascii=False, sort_keys=True) + '\n')
    _atomic_write(assignment_path(agent), f"{mission}:{task_id}\n")


def classify_assignment(agent, mission, task_id, generation):
    """公開中の assignment が「この実行のもの」かを判定する唯一の場所。

    generation:
      - 文字列 …… 呼び出し側が特定の実行 (card の started_at) を名指ししている。
        ロックを取る前に対象を決めた後始末は必ずこちらを使う。世代を証明でき
        ない場合は ASSIGN_UNVERIFIABLE を返し、決して「一致した」に倒さない。
      - None …… 呼び出し側は「いま card が示している実行」を対象にしている。
        同じロックの中で card を読んでから呼ぶ経路 (done / fail / update
        --reset) 専用。読みと書きの間に隙間が無く後任が割り込めないので、
        世代を問う必要がそもそも無い。
    """
    if agent_name_problem(agent):
        # 不正な名前の assignment は存在しえない。撤去側で die すると、card を
        # 書いたあとに落ちて片側だけ進むので、ここは「消さない」に倒す。
        return ASSIGN_UNVERIFIABLE
    # 「無い」= ASSIGN_ABSENT (撤去済み) と「読めない」= ASSIGN_UNVERIFIABLE
    # (証明できないので消さない) は別の結論である。分けるのは `ENOENT` の
    # 1 点だけで、それ以外の OSError・種類違い・デコード失敗はすべて
    # 「証明できない」側 (knowledge/empty-vs-unobservable.md §5)。
    #
    # 区別を `os.path.lexists()` で取らないのは、あれが `EACCES` でも False に
    # なるからである —— 親から実行権限が消えただけで「撤去済み」と読み、
    # **証明できない assignment の削除を許可する**。判定は読み取りが返す
    # `errno` に乗せる (memory: evidence-for-destructive-decisions)。
    published = _TASK_CARDS.read_regular_text_or_unreadable(assignment_path(agent))
    if _TASK_CARDS.is_missing(published):
        return ASSIGN_ABSENT
    if _TASK_CARDS.is_unreadable(published):
        return ASSIGN_UNVERIFIABLE
    published = published.strip()

    if published != f"{mission}:{task_id}":
        return ASSIGN_OTHER_TASK
    if generation is None:
        return ASSIGN_MINE

    identity = _read_assignment_identity(agent)
    if not identity:
        # 旧 plan.sh が書いた assignment には世代が無い。証拠が無いことを
        # 「一致した」に倒すと、まさに守りたかった後任の assignment を消す。
        return ASSIGN_UNVERIFIABLE
    if identity.get('mission') != mission or identity.get('task') != task_id:
        return ASSIGN_UNVERIFIABLE
    recorded = identity.get('started_at')
    if recorded is None or str(recorded) != str(generation):
        return ASSIGN_SUCCESSOR
    return ASSIGN_MINE


# ---------------------------------------------------------------------------
# 退役予約 — 退役中の Worker には assignment を公開しない
# ---------------------------------------------------------------------------
#
# watchdog の退役は「shutdown を伝える → 猶予 → SIGTERM → SIGKILL」で、決定と
# 実行の間に猶予期間ぶんの隙間がある。その隙間で Worker が自分で `plan.sh pull`
# を叩くと、**別の task を実行中の Worker にシグナルが飛ぶ**。pane の pid も
# created_at も変わらないので watchdog 側の identity チェックは通ってしまい、
# しかもその実行の後始末は古い退役要求の管轄外なので、新しい task は誰の管轄
# でもないまま in_progress で残る (Codex 5 巡目 P1-2)。
#
# dispatcher 側の除外 (t025) は「割り当てメッセージを送らない」だけで、Worker
# 自身の pull も、既に届いている指示も止められない。assignment を公開するのは
# ここ (キューロックの中) だけなので、予約を効かせる場所もここしかない。
#
# marker の中身は読まない。存在するかどうかだけを stat で見る — この判定は全
# Worker が叩く pull のロックの中に入るので、パースや列挙でロック保持時間を
# 伸ばしてはいけない。
RETIREMENT_SUFFIXES = ('.json', '.progress.json')


def retirement_reservation(agent):
    """`agent` に退役予約が立っているなら、その marker のパス。無ければ None。

    request (`<agent>.json`) と進行中 marker (`<agent>.progress.json`) の両方を
    見る。request だけを見ると、request が先に消える後始末の途中や、人間が
    request だけ消した状態で予約が外れてしまう。

    registry の場所は CREWVIA_REPO_ROOT を優先する。Worker が worktree 側の
    plan.sh を叩いた場合、REPO_ROOT (= スクリプトの位置) は worktree を指し、
    本体の registry を見逃す (memory: crewvia-worktree-repo-root-pitfall)。
    """
    if not agent or agent_name_problem(agent):
        return None
    root = os.environ.get('CREWVIA_REPO_ROOT') or REPO_ROOT
    base = os.path.join(root, 'registry', 'retirements')
    for suffix in RETIREMENT_SUFFIXES:
        path = os.path.join(base, agent + suffix)
        if os.path.exists(path):
            return path
    return None


def retire_assignment(agent, mission, task_id, generation):
    """assignment を撤去する唯一の入口。キューロック保持が前提。

    撤去するのは「この実行のもの」と確定したときだけ。返り値は
    classify_assignment() の判定そのもので、呼び出し側はそれを見て続行するか
    保留に倒すかを決める。
    """
    verdict = classify_assignment(agent, mission, task_id, generation)
    if verdict != ASSIGN_MINE:
        return verdict
    for path in (assignment_path(agent), assignment_identity_path(agent)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[plan.sh warn] failed to remove {path}: {e}", file=sys.stderr)
    return ASSIGN_MINE


def describe_assignment_verdict(agent, verdict):
    """撤去を見送った理由を人間に説明する 1 行。"""
    return {
        ASSIGN_ABSENT: f"assignment/{agent} は存在しません",
        ASSIGN_OTHER_TASK: f"assignment/{agent} は別の task を指しています",
        ASSIGN_SUCCESSOR: f"assignment/{agent} は同じ task の別の実行 (後任) のものです",
        ASSIGN_UNVERIFIABLE: (
            f"assignment/{agent} の実行世代を確認できません"
            f" (旧形式か破損 — 世代が読めない以上、後任のものでないと断定できません)"
        ),
    }.get(verdict, f"assignment/{agent}: {verdict}")


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def generate_slug(title):
    date = datetime.now(timezone.utc).strftime('%Y%m%d')
    ascii_part = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:30]
    if ascii_part:
        base = f"{date}-{ascii_part}"
    else:
        h = hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]
        base = f"{date}-{h}"
    # Avoid collisions
    slug = base
    n = 2
    while os.path.exists(mission_dir(slug)) or os.path.exists(os.path.join(ARCHIVE_DIR, slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Taskvia sync helpers (best-effort, standalone-compatible)
# ---------------------------------------------------------------------------

_TASKVIA_URL = os.environ.get('TASKVIA_URL', '').rstrip('/')
_TASKVIA_TOKEN = os.environ.get('TASKVIA_TOKEN', '')
_TASKVIA_TOKEN_WARNING_SHOWN = False


def _taskvia_request(method, path, payload=None):
    """HTTP call to Taskvia. Best-effort: never raises, logs warnings to stderr.
    Returns parsed response dict on success, None on failure.
    """
    global _TASKVIA_TOKEN_WARNING_SHOWN
    if _TASKVIA_URL and not _TASKVIA_TOKEN and not _TASKVIA_TOKEN_WARNING_SHOWN:
        print("[plan.sh] WARNING: TASKVIA_URL is set but TASKVIA_TOKEN is empty — Taskvia sync will be skipped.", file=sys.stderr)
        _TASKVIA_TOKEN_WARNING_SHOWN = True
    if not (_TASKVIA_URL and _TASKVIA_TOKEN):
        return None
    url = f"{_TASKVIA_URL}{path}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {_TASKVIA_TOKEN}',
    }
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode('utf-8', errors='replace')
        except Exception:
            body_text = '(unreadable)'
        print(f"[taskvia-sync] WARNING: {method} {path} HTTP {e.code}: {body_text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[taskvia-sync] WARNING: {method} {path} failed: {e}", file=sys.stderr)
        return None


def _taskvia_enabled():
    """Return True if Taskvia credentials are configured."""
    return bool(_TASKVIA_URL and _TASKVIA_TOKEN)


def _load_taskvia_map(map_path):
    """`queue/.taskvia-map.json` を、種類を確かめてから読む (t018)。

    倒す先は `{}` でよい —— 中身は「crewvia の task id → Taskvia の id」の
    キャッシュで、失われても次の同期が作り直す (冪等)。閉じているのは
    「素の `open()` が FIFO で返らない」ほうであって、空との取り違えではない。
    """
    text, problem = try_read_queue_file(map_path)
    if problem is not None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _taskvia_map_update(slug, task_id, status='pending'):
    """Update .taskvia-map.json after a successful inline sync.
    Keeps taskvia-sync.sh from re-registering tasks already pushed inline.
    """
    map_path = os.path.join(QUEUE_DIR, '.taskvia-map.json')
    task_map = _load_taskvia_map(map_path)
    map_key = f"{slug}:{task_id}"
    task_map[map_key] = {'registered': True, 'status': status}
    try:
        with open(map_path, 'w') as f:
            json.dump(task_map, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as e:
        print(f"[taskvia-sync] WARNING: .taskvia-map.json 更新失敗: {e}", file=sys.stderr)


def _taskvia_map_update_status(slug, task_id, status):
    """Update status of an existing .taskvia-map.json entry."""
    map_path = os.path.join(QUEUE_DIR, '.taskvia-map.json')
    task_map = _load_taskvia_map(map_path)
    map_key = f"{slug}:{task_id}"
    if map_key in task_map:
        task_map[map_key]['status'] = status
    else:
        task_map[map_key] = {'registered': True, 'status': status}
    try:
        with open(map_path, 'w') as f:
            json.dump(task_map, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as e:
        print(f"[taskvia-sync] WARNING: .taskvia-map.json 更新失敗: {e}", file=sys.stderr)


def _print_sync_summary(ok):
    """Print a one-line sync result to stdout (only when Taskvia is configured)."""
    if not _taskvia_enabled():
        return
    if ok:
        print("[taskvia-sync] ok")
    else:
        print("[taskvia-sync] failed — run scripts/taskvia-sync.sh to retry")


def taskvia_sync_init(slug, title):
    resp = _taskvia_request('POST', '/api/missions', {'slug': slug, 'title': title})
    return resp is not None


def taskvia_sync_add(slug, task_id, title, skills, priority, blocked_by):
    status = 'blocked' if blocked_by else 'pending'
    resp = _taskvia_request('POST', f'/api/missions/{slug}/tasks', {
        'id': task_id,
        'title': title,
        'status': status,
        'skills': skills,
        'priority': priority,
        'blocked_by': blocked_by,
    })
    if resp is not None:
        _taskvia_map_update(slug, task_id, status)
    return resp is not None


def taskvia_sync_pull(slug, task_id, assignee):
    resp = _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', {
        'status': 'in_progress',
        'assignee': assignee,
        'started_at': now_iso(),
    })
    if resp is not None:
        _taskvia_map_update_status(slug, task_id, 'in_progress')
    return resp is not None


def taskvia_sync_done(slug, task_id, result):
    resp = _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', {
        'status': 'done',
        'result': result,
        'completed_at': now_iso(),
    })
    if resp is not None:
        _taskvia_map_update_status(slug, task_id, 'done')
    _taskvia_unblock_dependents(slug, task_id)
    return resp is not None


def _taskvia_unblock_dependents(slug, completed_task_id):
    """completed_task_id の完了により blocked → pending に遷移すべきタスクを更新"""
    tasks = list_tasks(slug)
    done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
    done_ids.add(completed_task_id)
    for meta, _ in tasks:
        if meta.get('status') != 'pending':
            continue
        bb = meta.get('blocked_by') or []
        if not bb:
            continue
        if completed_task_id not in bb:
            continue
        if all(dep in done_ids for dep in bb):
            _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{meta["id"]}', {
                'status': 'pending',
            })
            _taskvia_map_update_status(slug, meta['id'], 'pending')


def taskvia_sync_archive(slug):
    resp = _taskvia_request('DELETE', f'/api/missions/{slug}')
    return resp is not None


def _load_workers_from_registry():
    """Parse registry/workers.yaml and return list of worker dicts."""
    registry_path = os.path.join(os.path.dirname(QUEUE_DIR), 'registry', 'workers.yaml')
    if not os.path.exists(registry_path):
        return []
    workers = []
    current = None
    # registry/workers.yaml も固定パスのガードを通す (Codex 9 巡目 P2)。
    # 素の open() だと、置き違えた FIFO 1 枚で Worker 同期が返らなくなる。
    text, problem = try_read_queue_file(registry_path)
    if problem is not None:
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- name:'):
            if current is not None:
                workers.append(current)
            name = stripped[len('- name:'):].strip().strip('"\'')
            current = {'name': name, 'skills': [], 'task_count': 0}
        elif current and re.match(r'\s+skills:', line):
            m = re.search(r'\[([^\]]*)\]', line)
            if m:
                inner = m.group(1).strip()
                current['skills'] = [s.strip() for s in inner.split(',')] if inner else []
        elif current and re.match(r'\s+task_count:', line):
            m = re.search(r'task_count:\s*(\d+)', line)
            if m:
                current['task_count'] = int(m.group(1))
        elif current and re.match(r'\s+role:', line):
            current['role'] = line.split(':', 1)[1].strip()
        elif current and re.match(r'\s+last_active:', line):
            current['last_active'] = line.split(':', 1)[1].strip()
    if current is not None:
        workers.append(current)
    return workers


def registered_worker(agent):
    """registry/workers.yaml の `agent` の項目 (無ければ None)。

    読めない registry を黙って「登録なし」にしない: 警告を出す (Director の判定が効かない
    ことを、pull した本人が見えるように)。判定自体は通す — registry の事故で全 Worker の
    pull を止めない。
    """
    if not agent:
        return None
    workers = _load_workers_from_registry()
    registry_path = os.path.join(os.path.dirname(QUEUE_DIR), 'registry', 'workers.yaml')
    if not workers and os.path.exists(registry_path):
        print(f"[plan.sh pull] WARNING: {registry_path} を読めない (または空) ため、"
              f"{agent!r} の role / skills を registry から確かめられませんでした",
              file=sys.stderr)
    for w in workers:
        if w.get('name') == agent:
            return w
    return None


def pull_skills(cli_value, registered):
    """pull が使う Worker の skills: `--skills` → 環境変数 `SKILLS` → registry の順。

    空集合 (どれも無い) は「絞り込みなし」ではなく、呼び出し側が拒否する。
    """
    for source in (cli_value, os.environ.get('SKILLS'),
                   ','.join((registered or {}).get('skills') or [])):
        skills = {s.strip() for s in (source or '').split(',') if s.strip()}
        if skills:
            return skills
    return set()


def taskvia_sync_workers():
    """Sync all workers from registry/workers.yaml to Taskvia."""
    workers = _load_workers_from_registry()
    if not workers:
        return True
    ok = True
    for w in workers:
        payload = {
            'name': w['name'],
            'skills': w.get('skills', []),
            'task_count': w.get('task_count', 0),
        }
        if w.get('role'):
            payload['role'] = w['role']
        if w.get('last_active'):
            payload['last_active'] = w['last_active']
        resp = _taskvia_request('POST', '/api/workers', payload)
        if resp is None:
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Argument parsing helper
# ---------------------------------------------------------------------------

#: サブコマンドごとの usage。`-h` / `--help` と、引数の誤りの両方がこれを出す。
#: 冒頭コメントの Usage と同じ内容 (tests/test_plan_strict_args.py が突き合わせる)。
USAGE = {
    'init': 'plan.sh init "<title>" [--mission <slug>] [--force]',
    'add': ('plan.sh add "<title>" [--mission <slug>] --skills <csv> [--blocked-by <csv>]\n'
            '                     [--priority high|medium|low] [--description <text>]\n'
            '                     [--target-dir <path>] [--idle-timeout <s>] [--max-timeout <s>]\n'
            '                     [--pr-number <N>]'),
    'pull': ('plan.sh pull [--mission <slug>] [--skills <csv>] [--agent <name>]\n'
             '                    [--target-dir <path>] [--task <task_id>]'),
    'done': 'plan.sh done <task_id> "<result>" [--mission <slug>]',
    'needs-director': 'plan.sh needs-director <task_id> "<理由>" [--mission <slug>]',
    'fail': ('plan.sh fail <task_id> [<handoff_path>] (--head <sha> | --no-head "<理由>")\n'
             '                     [--mission <slug>]'),
    'update': ('plan.sh update <task_id> [--mission <slug>] [--skills <csv>] [--blocked-by <csv>]\n'
               '                       [--priority high|medium|low] [--worker <name>] [--status <status>]\n'
               '                       [--description <text>] [--reset] [--pr-number <N>]'),
    'release-dep': 'plan.sh release-dep <task_id> [--dep <csv>] [--mission <slug>]',
    'retire': ('plan.sh retire <task_id> --agent <name> --started-at <generation>\n'
               '                       [--mission <slug>] [--outcome reset|needs-director]\n'
               '                       [--reason "<1 行>"] [--no-wait]'),
    'ready-for-verification': 'plan.sh ready-for-verification <task_id> [--mission <slug>]',
    'verify-result': ('plan.sh verify-result <task_id> <pass|fail|needs_human_review>\n'
                      '                            [--mission <slug>] [--notes "<text>"]'),
    'review': 'plan.sh review <mission_slug>',
    'launch': 'plan.sh launch <mission_slug>',
    'task-graph': 'plan.sh task-graph',
    'lint': 'plan.sh lint [<mission_slug> | --mission <slug>] [--strict]',
    'status': 'plan.sh status [--mission <slug>] [--all]',
    'archive': 'plan.sh archive <slug>',
    'resync': 'plan.sh resync (<slug> | --all)',
    'dashboard-data': 'plan.sh dashboard-data [--all]',
    'resolve-mission': 'plan.sh resolve-mission <task_id> [--mission <slug>]',
}

#: サブコマンドごとに受け付ける positional の数 (最小, 最大)。余剰は黙って捨てず拒否する
#: (`done t007 --agent X "結果"` で Result が `--agent` になった事故 / add の title に
#: 引数が紛れた事故 — どちらも余った・取り違えた positional を黙って受けたのが根)。
#: dispatch テーブルと同じキーを持つこと (tests/test_plan_strict_args.py が突き合わせる)。
POSITIONAL_ARITY = {
    'init': (1, 1), 'add': (1, 1), 'pull': (0, 0), 'done': (2, 2),
    'needs-director': (2, 2), 'fail': (1, 2), 'update': (1, 1), 'release-dep': (1, 1),
    'retire': (1, 1), 'ready-for-verification': (1, 1), 'verify-result': (2, 2),
    'review': (1, 1), 'launch': (1, 1), 'task-graph': (0, 0), 'lint': (0, 1),
    'status': (0, 0), 'archive': (1, 1), 'resync': (0, 1), 'dashboard-data': (0, 0),
    'resolve-mission': (1, 1),
}

#: 使い方の誤りの終了コード。`pull` だけは 1 — `pull` の 2 は「タスクなし (idle)」で、
#: Worker は 2 を受けると 30 秒待って再試行する (agents/worker.md)。引数の誤りを 2 で返すと
#: 「壊れた呼び出し」が「ただのアイドル」として無限に再試行される。1 = 実エラー
#: (不正引数を含む、と worker.md が明記している)。
USAGE_EXIT = 2
PULL_USAGE_EXIT = 1


class UsageExit(SystemExit):
    """`-h` / 使い方の誤りによる終了。**何も書かずに**終わったことの印。

    末尾の dispatch は SystemExit のあとで task-graph を再生成する (途中まで書いて die() した
    実行の後でも DAG を最新にするため)。`--help` はそこで registry/ に 1 バイトも書いては
    ならないので、この型だけは再生成を飛ばす。
    """


#: `-x` / `--xxx` の形の語 (`--x=y` を含む)。**空白を含む語は option ではない** — Result や
#: title が `- 修正した` や `-3 件` のように `-` で始まるだけで拒否されないように。
_OPTION_LIKE = re.compile(r'^--?[A-Za-z][A-Za-z0-9_-]*(=.*)?$')


def _usage_text():
    return 'Usage: ' + USAGE.get(SUBCOMMAND, f'plan.sh {SUBCOMMAND} [args...]')


def _usage_exit(message):
    """使い方の誤り: メッセージと usage を stderr に出して終わる。何も書いていない。"""
    code = PULL_USAGE_EXIT if SUBCOMMAND == 'pull' else USAGE_EXIT
    print(f"plan.sh {SUBCOMMAND}: {message}", file=sys.stderr)
    print(_usage_text(), file=sys.stderr)
    raise UsageExit(code)


def _ensure_queue_dirs():
    """queue の骨組み。引数を検証し終えたあとにだけ作る (`--help` は何も作らない)。"""
    os.makedirs(os.path.join(QUEUE_DIR, 'missions'), exist_ok=True)
    os.makedirs(os.path.join(QUEUE_DIR, 'archive'), exist_ok=True)


def parse_opts(args, spec):
    """spec: dict of {flag: 'value' or 'bool'}. Returns (opts dict, positional list).

    厳格: 未知の option (`-` で始まる option らしい語) は拒否する。`--` 以降は全部 positional
    (`-` で始まる title / Result の逃げ道)。`-h` / `--help` は usage を出して exit 0 —
    どちらも queue / registry に 1 バイトも書かない。positional の数は
    POSITIONAL_ARITY で宣言し、過不足は拒否する。
    """
    opts = {}
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--':
            positional.extend(args[i + 1:])
            break
        if a in ('-h', '--help'):
            print(_usage_text())
            raise UsageExit(0)
        if a in spec:
            kind = spec[a]
            if kind == 'value':
                if i + 1 >= len(args):
                    _usage_exit(f"option {a} requires a value")
                value = args[i + 1]
                if value in spec or _OPTION_LIKE.match(value):
                    # `--mission --skills x` — 値を打ち忘れて次の option を値に食った
                    # (空白を含む値 `--not-an-option text` は option の形ではないので通る)
                    _usage_exit(f"option {a} requires a value (got the option {value})")
                opts[a] = value
                i += 2
            else:
                opts[a] = True
                i += 1
        elif _OPTION_LIKE.match(a):
            hint = ""
            if '=' in a and a.split('=', 1)[0] in spec:
                hint = f" (`{a.split('=', 1)[0]} <値>` と空白で区切ること)"
            _usage_exit(
                f"unknown option {a!r}{hint}\n"
                f"  `-` で始まる値を positional として渡すなら `--` の後ろに置くこと")
        else:
            positional.append(a)
            i += 1
    low, high = POSITIONAL_ARITY.get(SUBCOMMAND, (0, len(positional)))
    if not low <= len(positional) <= high:
        want = f"{low}" if low == high else f"{low}〜{high}"
        _usage_exit(
            f"expected {want} positional argument(s), got {len(positional)}: {positional}\n"
            f"  空白を含む値 (title / result / reason) は 1 つの引数として引用符で囲むこと")
    _ensure_queue_dirs()
    return opts, positional


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args):
    opts, positional = parse_opts(args, {'--mission': 'value', '--force': 'bool'})
    if not positional:
        die("init requires a mission title")
    title = positional[0]
    force = opts.get('--force', False)
    sync_holder = [None]  # (slug, title)

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if slug:
            existing = os.path.exists(mission_dir(slug))
            if existing and not force:
                die(f"mission '{slug}' already exists. Use --force to overwrite.")
            if existing:
                # Non-destructive overwrite: move the previous mission to
                # archive/<slug>.overwritten-<timestamp>/ instead of rm -rf,
                # so worker output is never silently lost.
                ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                backup_name = f"{slug}.overwritten-{ts}"
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                shutil.move(mission_dir(slug), os.path.join(ARCHIVE_DIR, backup_name))
                print(
                    f"[plan.sh init] previous '{slug}' moved to archive/{backup_name}",
                    file=sys.stderr,
                )
                # Drop the slug from active_missions so save_state doesn't
                # leave a dangling reference if the rebuild below fails.
                active = state.get('active_missions') or []
                if slug in active:
                    active.remove(slug)
                state['active_missions'] = active
                if state.get('default_mission') == slug:
                    state['default_mission'] = active[0] if active else None
        else:
            slug = generate_slug(title)

        os.makedirs(tasks_dir(slug), exist_ok=True)
        mission = {
            'title': title,
            'slug': slug,
            'status': 'drafting',
            'created_at': now_iso(),
            'completed_at': None,
            'next_task_id': 1,
            'max_review_cycles': 3,
            'review': {
                'last_verdict': None,
                'cycle_count': 0,
                'reviewed_at': None,
                'reviewer': None,
            },
        }
        save_mission(slug, mission)

        active = state.get('active_missions') or []
        if slug not in active:
            active.append(slug)
        state['active_missions'] = active
        state['default_mission'] = slug
        save_state(state)
        sync_holder[0] = (slug, title)
        print(f"Initialized mission: {slug}")
        print(f"  title: {title}")
        print(f"  path:  {mission_dir(slug)}")

    with_lock(_do)
    if sync_holder[0]:
        ok = taskvia_sync_init(*sync_holder[0])
        _print_sync_summary(ok)
        taskvia_sync_workers()


def cmd_add(args):
    opts, positional = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--blocked-by': 'value',
        '--priority': 'value',
        '--description': 'value',
        '--target-dir': 'value',
        '--idle-timeout': 'value',
        '--max-timeout': 'value',
        '--pr-number': 'value',
    })
    if not positional:
        die("add requires a task title")
    title = positional[0]
    sync_holder = [None]  # (slug, task_id, title, skills, priority, blocked_by)

    skills = [s.strip() for s in opts.get('--skills', '').split(',') if s.strip()]
    if not skills:
        die("--skills is required. Dispatcher cannot assign tasks without skills.")
    blocked_by = [s.strip() for s in opts.get('--blocked-by', '').split(',') if s.strip()]
    priority = opts.get('--priority', 'medium')
    if priority not in PRIORITY_ORDER:
        die(f"invalid priority '{priority}'. Use high|medium|low.")
    description = opts.get('--description', '')

    # --target-dir: Worker が起動時に cd する target project のパス。
    # 未指定なら None (crewvia 本体を触るタスク扱い)。
    # ~ 展開と絶対パス化をかけ、ディレクトリ存在確認を入れる。
    target_dir = opts.get('--target-dir')
    if target_dir:
        target_dir = os.path.abspath(os.path.expanduser(target_dir))
        if not os.path.isdir(target_dir):
            die(f"--target-dir does not exist or is not a directory: {target_dir}")
    else:
        target_dir = None

    # --pr-number: codex-review skill task で dispatcher が kai-review.sh に
    # 渡すための PR 番号。指定しない場合 None (frontmatter に pr_number 行を出さない)。
    pr_number = None
    if opts.get('--pr-number'):
        try:
            pr_number = int(opts['--pr-number'])
        except ValueError:
            die("--pr-number must be a positive integer")
        if pr_number <= 0:
            die("--pr-number must be a positive integer")

    # --idle-timeout / --max-timeout: タスクごとの timeout 秒数（省略可）
    timeout = {}
    if opts.get('--idle-timeout'):
        try:
            timeout['idle'] = int(opts['--idle-timeout'])
        except ValueError:
            die("--idle-timeout must be an integer number of seconds")
    if opts.get('--max-timeout'):
        try:
            timeout['max'] = int(opts['--max-timeout'])
        except ValueError:
            die("--max-timeout must be an integer number of seconds")
    timeout = timeout if timeout else None

    def _do():
        state = load_state()
        slug = opts.get('--mission') or state.get('default_mission')
        if not slug:
            die("no active mission. Run 'plan.sh init' first or pass --mission.")
        if not os.path.exists(mission_dir(slug)):
            die(f"mission '{slug}' not found.")

        mission = load_mission(slug)
        task_num = int(mission.get('next_task_id') or 1)
        task_id = f"t{task_num:03d}"

        meta = {
            'id': task_id,
            'title': title,
            'skills': skills,
            'priority': priority,
            'status': 'pending',
            'blocked_by': blocked_by,
            'target_dir': target_dir,
            'worker': None,
            'started_at': None,
            'completed_at': None,
        }
        if timeout:
            meta['timeout'] = timeout
        if pr_number is not None:
            meta['pr_number'] = pr_number
        body = build_task_body(description, '')
        save_task(slug, task_id, meta, body)
        sync_holder[0] = (slug, task_id, title, skills, priority, blocked_by)

        mission['next_task_id'] = task_num + 1
        save_mission(slug, mission)
        suffix = f" [target: {target_dir}]" if target_dir else ""
        print(f"Added: {slug}/{task_id} — {title}{suffix}")

    with_lock(_do)
    if sync_holder[0]:
        ok = taskvia_sync_add(*sync_holder[0])
        _print_sync_summary(ok)


def mission_search_order(explicit, state):
    """`--mission` を省略したときに、どの mission をどの順で探すか。

    `--mission` があればそれだけ。無ければ active mission を **default_mission 優先** で。
    `pull` と `resolve-mission` の **唯一の定義** (t026): kai-review.sh が拒否記録を書く先の
    mission と、`plan.sh pull --task` が実際に task を取る mission が食い違うと、記録は
    別 mission の名前で書かれ、dispatcher は見つけられずに #11 の再 spawn ループが戻る。
    別々に解決しない。
    """
    if explicit:
        return [explicit]
    slugs = list(state.get('active_missions') or [])
    default = state.get('default_mission')
    if default and default in slugs:
        slugs.remove(default)
        slugs.insert(0, default)
    return slugs


#: task id が複数 mission に当たったときに、環境変数で mission を決めてよい status。
#: Worker が今その task を実行している (または検証中の) 間だけ。
_ENV_MISSION_STATUSES = ('in_progress', 'verifying')


def _env_mission_for_task(task_id, matches):
    """`CREWVIA_MISSION_SLUG` が指す mission を、**自分の担当** と確かめられたときだけ返す。

    env だけを信じると、別の mission を担当していたときの残りの env (worktree の
    `.crewvia-env` を source したままのシェル) が、無関係な mission の同じ tNNN に届く。
    だから 3 つ全部を要求する: (1) その mission が候補に居る (2) card の worker が
    `AGENT_NAME` と一致 (3) card の status が in_progress / verifying。
    読めない card は「確かめられなかった」なので使わない (= 拒否の側に倒す)。
    """
    env_slug = os.environ.get('CREWVIA_MISSION_SLUG', '').strip()
    agent = os.environ.get('AGENT_NAME', '').strip()
    if not env_slug or not agent or env_slug not in matches:
        return None
    try:
        meta, _ = load_task(env_slug, task_id)
    except (Exception, SystemExit):
        return None
    if meta.get('worker') != agent or meta.get('status') not in _ENV_MISSION_STATUSES:
        return None
    return env_slug


def resolve_ambiguous_mission(command, task_id, matches):
    """`--mission` 省略で task id が複数 mission に当たったときの **唯一の解決**。

    done / fail / needs-director / update / ready-for-verification / verify-result が使う。
    task id は mission ごとの採番なので、Worker が取り違えて別 mission の同じ tNNN を
    書き換える事故を、拒否 (候補と打つべきコマンドを添えて) で潰す。ただし自分の担当と
    確かめられる mission が env にあるときだけ、それを使う (使ったことを stderr に 1 行)。
    """
    slug = _env_mission_for_task(task_id, matches)
    if slug:
        print(
            f"[plan.sh {command}] --mission 省略: CREWVIA_MISSION_SLUG={slug} を使います "
            f"({task_id} の worker={os.environ.get('AGENT_NAME')} が AGENT_NAME と一致し実行中)",
            file=sys.stderr,
        )
        return slug
    lines = [f"task '{task_id}' exists in multiple missions: {matches}. Use --mission."]
    lines.append("  どの mission か決められません。打つコマンド (引数は元のまま、--mission を足す):")
    for s in matches:
        lines.append(f"    plan.sh {command} {task_id} --mission {s} ...")
    die("\n".join(lines))


def cmd_resolve_mission(args):
    """plan.sh resolve-mission <task_id> [--mission <slug>]

    その task が属する mission の slug を **1 行だけ** stdout に出す (読み取り専用)。
    `--mission` を省略したときの規則は `pull` と同じ (`mission_search_order()`):
    default_mission 優先で active mission を探し、最初に task を持つものを採る。
    kai-review.sh が「実効 mission」を pull の **前に** 1 度だけ解決して保持するのに使う (t026)。
    見つからなければ exit 1 (stdout には何も出さない)。
    """
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) != 1:
        die("resolve-mission requires exactly one <task_id>\\n"
            "Usage: plan.sh resolve-mission <task_id> [--mission <slug>]")
    task_id = positional[0]
    state = load_state()
    slugs = mission_search_order(opts.get('--mission'), state)
    for slug in slugs:
        if os.path.exists(task_path(slug, task_id)):
            print(slug)
            return
    die(f"task '{task_id}' not found in mission(s): {slugs}")


def cmd_pull(args):
    opts, _ = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--agent': 'value',
        '--target-dir': 'value',
        '--task': 'value',   # specific task ID (dispatcher-assigned; bypasses skill/target/blocked filters)
    })
    agent = opts.get('--agent') or os.environ.get('AGENT_NAME', '')
    specific_task = opts.get('--task')
    if agent:
        require_valid_agent_name(agent)

    # Director は pull しない。判定は registry 上の role で — **`ROLE` 環境変数は見ない**:
    # dispatcher が spawn する kai-review.sh は Director の env を継承しうるので、env で
    # 判定すると Kai-codex が Director として拒否される (memory: crewvia-director-pull-pitfall)。
    registered = registered_worker(agent)
    if registered and registered.get('role', '').strip('"\' ').lower() == 'director':
        die(f"{agent!r} は registry/workers.yaml で role: director です。Director は task を pull しません "
            f"(Worker に割り当てる側)。`plan.sh status` で状態を見るか、Worker として起動し直してください。")

    requested_skills = pull_skills(opts.get('--skills'), registered)
    if not requested_skills:
        die("pull requires the worker's skills: pass --skills <csv>, set the SKILLS environment "
            "variable, or register the agent's skills in registry/workers.yaml. "
            "(skills を空にすると skill の絞り込みが丸ごと無効になるので、黙って進めません)")

    # Determine effective target_dir for filtering:
    # --target-dir flag > TARGET_DIR env var > None (crewvia-local)
    explicit_td = opts.get('--target-dir')
    if explicit_td:
        effective_target = os.path.abspath(os.path.expanduser(explicit_td))
    else:
        env_td = os.environ.get('TARGET_DIR', '').strip()
        effective_target = os.path.abspath(env_td) if env_td else None

    chosen_holder = [None]
    diag = {'reason': None, 'detail': ''}

    def _do():
        # ロックの中で最初に見る。退役予約が立っている Worker には、この pull が
        # 1 バイトも書かずに引き返す — card を in_progress にしてから気付くと、
        # まさにこの PR が潰した「割れたトランザクション」を自分で作ることになる。
        reserved = retirement_reservation(agent)
        if reserved:
            diag['reason'] = 'retirement_reserved'
            diag['detail'] = (
                f'Worker {agent!r} は退役処理の対象です ({reserved})。'
                f'退役が終わるか、marker を手で削除するまで新しい task は割り当てません'
            )
            return

        state = load_state()
        slugs = mission_search_order(opts.get('--mission'), state)

        if not slugs:
            diag['reason'] = 'no_active_missions'
            diag['detail'] = 'state.yaml lists no active missions'
            return

        if specific_task and not opts.get('--mission'):
            # --task は「この task を取れ」なので、どの mission か曖昧なまま最初に当たった
            # mission で進めない (別 mission の同じ tNNN を in_progress にしてしまう)。
            holders = [s for s in slugs if os.path.exists(task_path(s, specific_task))]
            if len(holders) > 1:
                die(
                    f"task '{specific_task}' exists in multiple missions: {holders}. "
                    f"--mission が無いので決められません。打つコマンド:\n"
                    + "\n".join(f"    plan.sh pull --task {specific_task} --mission {s}" for s in holders)
                )

        # Diagnostic counters per slug
        scanned = 0
        pending_count = 0
        skill_mismatch = 0
        blocked_count = 0
        held_tasks = []   # [(slug, task_id, [failed dep ids])] — Director の判断待ち。
                          # slug を持つのは、別 mission に同じ tNNN があるため (解除の案内に要る)
        target_mismatch = 0
        missing_dirs = []

        candidates = []
        for slug in slugs:
            if not os.path.exists(mission_dir(slug)):
                missing_dirs.append(slug)
                continue
            tasks = list_tasks(slug)
            scanned += len(tasks)
            done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
            task_statuses = {m['id']: m.get('status') for (m, _) in tasks}
            for (meta, body) in tasks:
                # --task: match by ID; skill/target filters are bypassed (dispatcher
                # has already verified them), but blocked_by is always enforced as
                # a defense-in-depth guard against dispatcher races or bugs.
                if specific_task:
                    if meta.get('id') != specific_task:
                        continue
                    st = meta.get('status')
                    if st in TERMINAL_STATUSES:
                        die(f"task '{specific_task}' is already {st} (use plan.sh status to review)")
                    if st == 'in_progress':
                        die(
                            f"task '{specific_task}' is already in_progress "
                            f"(assigned to {meta.get('worker', '?')}). "
                            f"If the worker crashed, reset the task status manually."
                        )
                    if st != 'pending':
                        die(f"task '{specific_task}' has unexpected status: {st}")
                    # Defense-in-depth: reject pull if any dependency is not yet done,
                    # even when --task bypasses skill/target filters.  This prevents
                    # a blocked task from being executed when the dispatcher sends a
                    # stale kickoff message (e.g. blocked_by race, parse glitch).
                    # cancelled deps are excluded (Director's own decision).  A
                    # *failed* dep is HELD until the Director releases it
                    # (plan.sh release-dep): treating it as satisfied let a review
                    # task run right after its QA failed (t007 / backlog #9).
                    verdict = card_dependencies(meta, done_ids, task_statuses)
                    unmet = verdict.unmet
                    if verdict.held:
                        die(
                            f"task '{specific_task}' is held: {held_dependency_hint(specific_task, verdict.held, slug)}"
                            f" — cannot pull until the Director decides "
                            f"(blocked by unfinished dependencies: {unmet})"
                        )
                    if unmet:
                        die(
                            f"task '{specific_task}' is blocked by unfinished dependencies: "
                            f"{unmet} — cannot pull until all blocked_by tasks are done. "
                            f"(dispatcher should not have assigned this task yet)"
                        )
                    # Warn if worker skills don't fully cover the task's required skills
                    task_req = set(meta.get('skills') or [])
                    worker_skills = {s.strip() for s in requested_skills}
                    missing = task_req - worker_skills
                    if task_req and missing:
                        print(
                            f"WARNING: task '{specific_task}' requires skills {sorted(task_req)} "
                            f"but worker has {sorted(worker_skills) or '<none>'}; "
                            f"missing: {sorted(missing)}",
                            file=sys.stderr,
                        )
                    candidates.append((slug, meta, body))
                    break  # task IDs are unique within a mission
                # Regular auto-selection flow
                if meta.get('status') != 'pending':
                    continue
                pending_count += 1
                if requested_skills and not set(meta.get('skills', [])).issubset(requested_skills):
                    skill_mismatch += 1
                    continue
                # Target-dir filtering:
                # (1) effective_target is None AND task target_dir is None → match (crewvia-local)
                # (2) effective_target is set AND task target_dir matches   → match
                # (3) otherwise → skip
                task_td = meta.get('target_dir')
                if effective_target is None:
                    if task_td is not None:
                        target_mismatch += 1
                        continue
                else:
                    if task_td != effective_target:
                        target_mismatch += 1
                        continue
                # cancelled deps do not block (Director's own decision).  failed
                # deps HOLD the task until the Director releases them.
                verdict = card_dependencies(meta, done_ids, task_statuses)
                if verdict.unmet:
                    blocked_count += 1
                    if verdict.held:
                        held_tasks.append((slug, meta.get('id'), verdict.held))
                    continue
                candidates.append((slug, meta, body))

        if missing_dirs:
            print(
                f"[plan.sh pull] WARNING: state.yaml references missing mission "
                f"directories: {missing_dirs}",
                file=sys.stderr,
            )

        if not candidates:
            if specific_task:
                die(f"task '{specific_task}' not found in mission(s): {slugs}")
            if pending_count == 0:
                diag['reason'] = 'no_pending_tasks'
                diag['detail'] = f'{scanned} task(s) scanned across {len(slugs)} mission(s); none pending'
            elif skill_mismatch and not blocked_count and not target_mismatch:
                diag['reason'] = 'no_skill_match'
                diag['detail'] = (
                    f'{pending_count} pending task(s) found but none match skills '
                    f'{sorted(requested_skills) or "<any>"}'
                )
            elif target_mismatch and not skill_mismatch and not blocked_count:
                diag['reason'] = 'no_target_match'
                target_label = effective_target or '(crewvia-local)'
                diag['detail'] = (
                    f'{pending_count} pending task(s) found but none match target_dir '
                    f'{target_label!r}'
                )
            elif blocked_count and not skill_mismatch and not target_mismatch:
                diag['reason'] = 'all_blocked'
                diag['detail'] = f'{blocked_count} pending task(s) blocked by unmet dependencies'
                if held_tasks:
                    diag['detail'] += ' — ' + '; '.join(
                        f"[{s}/{t}] " + held_dependency_hint(t, h, s) for s, t, h in held_tasks)
            else:
                diag['reason'] = 'no_eligible_task'
                diag['detail'] = (
                    f'{pending_count} pending; {skill_mismatch} skill-mismatch; '
                    f'{target_mismatch} target-mismatch; {blocked_count} blocked'
                )
                if held_tasks:
                    diag['detail'] += ' — ' + '; '.join(
                        f"[{s}/{t}] " + held_dependency_hint(t, h, s) for s, t, h in held_tasks)
            return

        # Priority-first sort: high-priority tasks across all active missions
        # win before any lower-priority work, regardless of which mission they
        # live in. Default-mission ordering is only a tiebreaker.
        slug_index = {s: i for i, s in enumerate(slugs)}
        candidates.sort(key=lambda c: (
            PRIORITY_ORDER.get(c[1].get('priority', 'medium'), 1),
            slug_index.get(c[0], 999),
            c[1].get('id', ''),
        ))

        slug, meta, body = candidates[0]
        meta['status'] = 'in_progress'
        meta['worker'] = agent or None
        meta['started_at'] = now_generation()
        save_task(slug, meta['id'], meta, body)

        # assignment の公開は card の書き換えと同じトランザクションで行う。
        # ロックの外に出すと、(a) card が in_progress なのに assignment が
        # 無い瞬間が生まれて dispatcher に idle と誤認され、(b) 後始末側の
        # 「判定してから消す」と直列化できなくなる。
        if agent:
            publish_assignment(agent, slug, meta['id'], meta['started_at'])

        desc, _result = parse_task_body(body)
        chosen_holder[0] = {
            'mission': slug,
            'id': meta['id'],
            'title': meta['title'],
            'description': desc,
            'skills': meta.get('skills') or [],
            'priority': meta.get('priority', 'medium'),
            'blocked_by': meta.get('blocked_by') or [],
            'target_dir': meta.get('target_dir'),  # None for crewvia-local tasks
        }

    with_lock(_do)

    if chosen_holder[0] is None:
        # exit 2 = "no task available" (idle / sleep & retry)
        # exit 1 is reserved for real errors raised via die()
        print(
            f"[plan.sh pull] no task available: {diag['reason']} — {diag['detail']}",
            file=sys.stderr,
        )
        sys.exit(2)

    # assignment は _do() の中 (キューロック内) で公開済み。ここから先の
    # Taskvia sync / worktree 作成は subprocess や HTTP を伴うので、ロックを
    # 抱えたまま実行してはいけない。
    ok = taskvia_sync_pull(chosen_holder[0]['mission'], chosen_holder[0]['id'], agent)
    _print_sync_summary(ok)

    # Derive a URL-safe task slug from the title for worktree naming
    def _slugify(title, fallback):
        ascii_only = re.sub(r'[^\x00-\x7F]+', ' ', title)
        normalized = re.sub(r'[^a-zA-Z0-9]+', ' ', ascii_only)
        parts = [p.lower() for p in normalized.split() if p]
        slug = '-'.join(parts)[:40].rstrip('-')
        return slug or fallback

    task_id = chosen_holder[0]['id']
    mission_slug = chosen_holder[0]['mission']
    task_slug = _slugify(chosen_holder[0]['title'], task_id)
    worktree_path = None
    task_target_dir = chosen_holder[0].get('target_dir')

    git_helpers = os.path.join(REPO_ROOT, 'scripts', 'git-helpers.sh')
    if not task_target_dir and os.path.exists(git_helpers):
        wt_cmd = (
            f'source {shlex.quote(git_helpers)} && '
            f'crewvia_create_worktree {shlex.quote(mission_slug)} '
            f'{shlex.quote(task_id)} {shlex.quote(task_slug)}'
        )
        wt = subprocess.run(
            ['bash', '-c', wt_cmd],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if wt.returncode == 0:
            worktree_path = wt.stdout.strip()
            env_file = os.path.join(worktree_path, '.crewvia-env')
            with open(env_file, 'w') as _ef:
                _ef.write(f'export CREWVIA_MISSION_SLUG={shlex.quote(mission_slug)}\n')
                _ef.write(f'export CREWVIA_TASK_ID={shlex.quote(task_id)}\n')
                _ef.write(f'export CREWVIA_TASK_SLUG={shlex.quote(task_slug)}\n')
        else:
            print(
                f'[plan.sh pull] WARNING: worktree creation skipped:\n{wt.stderr.strip()}',
                file=sys.stderr,
            )

    chosen_holder[0]['task_slug'] = task_slug
    chosen_holder[0]['worktree_path'] = worktree_path

    print(json.dumps(chosen_holder[0], ensure_ascii=False))


# ---------------------------------------------------------------------------
# QA Gate validation helpers
# ---------------------------------------------------------------------------

def _validate_qa_gate(result_text, has_qa_checkpoints):
    """QA Gate セクションを解析して FAIL 理由を返す（空リスト = PASS）。

    Director Fix 1:
      - has_qa_checkpoints=True (frontmatter に qa_checkpoints あり)
          → ## QA Gate セクションは必須。欠落 = FAIL
      - has_qa_checkpoints=False (qa_checkpoints なし)
          → 後方互換: セクションがなければスキップ、あれば検証する
    """
    section_match = re.search(r'^##\s+QA\s+Gate\s*$', result_text, re.MULTILINE | re.IGNORECASE)
    if not section_match:
        if has_qa_checkpoints:
            return [
                "  [missing] ## QA Gate セクションがありません。"
                " qa_checkpoints が宣言されたタスクでは必須です。"
            ]
        # qa_checkpoints 未宣言の既存タスク → スキップ（後方互換）
        return []

    section_body = result_text[section_match.end():]

    fails = []
    pattern = re.compile(
        r'checkpoint:\s*(?P<name>[^|]+?)\s*\|\s*required:\s*(?P<req>yes|no)\s*\|'
        r'\s*result:\s*(?P<res>observed|not_run|failed)',
        re.IGNORECASE,
    )
    found_any = False
    for m in pattern.finditer(section_body):
        found_any = True
        req = m.group('req').lower()
        res = m.group('res').lower()
        name = m.group('name').strip()
        if req == 'yes' and res != 'observed':
            fails.append(f"  [{res}] {name}")

    if has_qa_checkpoints and not found_any:
        fails.append(
            "  [missing] ## QA Gate セクションにチェックポイント行がありません。"
            " 形式: checkpoint: <名前> | required: yes | result: observed"
        )

    return fails


def _validate_required_evidence(result_text, required):
    """required_evidence パターンの存在チェック。見つからないパターンのリストを返す。

    Director Fix 2: [EVIDENCE EXEMPTION: ...] は実装しない。
    証拠が出せない場合は Director が required_evidence: [] で作成するか、
    Worker が plan.sh needs-director を使うこと。
    """
    missing = []
    for pattern in (required or []):
        if pattern and pattern not in result_text:
            missing.append(f"  '{pattern}'")
    return missing


def _git_head_probe_dirs():
    """head を解決してよい repo の候補 (呼び出し元の cwd が先頭)。

    Worker の worktree / TARGET_DIR の別 repo / crewvia 本体。worktree は主 repo と
    object DB を共有するので、どれかで解決できれば「実在する commit」と言える。
    """
    dirs = []
    for d in (os.getcwd(), os.environ.get('TARGET_DIR', '').strip(),
              os.environ.get('CREWVIA_REPO_ROOT', '').strip(), REPO_ROOT):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def _resolve_head_commit(head):
    """head (SHA / 略称) を実在する commit の完全な SHA に解決する。

    戻り値: (full_sha, None) か (None, 理由)。**解決できなかった理由を必ず返す** —
    「git が無い / repo でない」を「検証しない」に倒さないため。
    """
    if not re.fullmatch(r'[0-9a-fA-F]{7,64}', head or ''):
        return None, (f"--head '{head}' は commit SHA の形 (16 進 7〜64 桁) ではありません"
                      " (`git rev-parse HEAD` の出力を渡してください)")
    in_repo = False
    for d in _git_head_probe_dirs():
        try:
            inside = subprocess.run(
                ['git', '-C', d, 'rev-parse', '--is-inside-work-tree'],
                capture_output=True, text=True, timeout=10)
            if inside.returncode != 0:
                continue
            in_repo = True
            found = subprocess.run(
                ['git', '-C', d, 'rev-parse', '--verify', '--quiet', f'{head}^{{commit}}'],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        full = found.stdout.strip()
        if found.returncode == 0 and re.fullmatch(r'[0-9a-f]{40,64}', full):
            return full, None
    if in_repo:
        return None, (f"--head '{head}' は、この repo に存在する commit ではありません"
                      " (打ち間違い / 別 repo の SHA / 曖昧な略称)")
    return None, ("git repo の中で実行されていないため --head を確かめられません"
                  " (検証対象が git 管理外なら --no-head \"<理由>\" で明示すること)")


def _handoff_names_head(handoff_path, full_sha):
    """handoff が **この head** について書かれていることの確認。(ok, 説明)

    古い handoff (別 head 時点) の再提出を弾く。handoff 内に、完全な SHA の先頭と
    一致する 16 進の語 (7 桁以上) が 1 つでもあれば通す。history として複数の SHA を
    並べた handoff も通るので、これは「別 head 専用の handoff をそのまま出す」を
    止めるための確認であって、内容の真正性の証明ではない。

    ファイルが無い (ENOENT) ときは通す: 無いものは「古い再提出」になり得ず、
    dispatcher 側も読めなければ警告する (ok=True, 説明=警告文)。
    読めない (ENOENT 以外) ときは **通さない** — 観測できなかったことを
    「結び付いている」の根拠にしない。
    """
    path = os.path.abspath(handoff_path)
    text = _TASK_CARDS.read_regular_text_or_unreadable(path)
    if _TASK_CARDS.is_missing(text):
        return True, (f"handoff ファイルがまだ存在しません: {path}"
                      " (Director への通知が中身なしになります)")
    if _TASK_CARDS.is_unreadable(text):
        return False, f"handoff を読めないため、この head に結び付いているか確かめられません: {path}"
    for token in re.findall(r'[0-9a-fA-F]{7,64}', text):
        if full_sha.startswith(token.lower()):
            return True, None
    return False, (f"handoff ({path}) が、報告する head {full_sha[:12]} に触れていません。"
                   " 別の head 時点の古い handoff を再提出していませんか?"
                   " handoff に検証した head (`git rev-parse HEAD`) を書き足すか、"
                   "今の作業で書き直してください")


def _validate_fail_evidence(meta, report):
    """FAIL の証拠を検証する。(エラー文 or None, card に書く欄の dict) を返す。

    **done の検証 (`_validate_qa_gate` / `_validate_required_evidence`) は流用しない。**
    それらは「PASS の証拠が揃っていること」を要求する。FAIL の理由が「PASS の証拠が
    出せない」ことであるのは普通で、流用すると required な checkpoint が
    `failed` / `not_run` の FAIL — つまり最も正当な FAIL — が報告できなくなる
    (別の outage)。qa_checkpoints / required_evidence を宣言した task でも、
    FAIL に課す証拠は宣言の有無で変えない: **検証対象の head (commit SHA)** と、
    handoff を付けるならその head に結び付いていること。

    証拠を出せない報告者 (検証対象が git 管理外など) のために `--no-head "<理由>"` がある。
    Director が `required_evidence: []` を置くのと同じく、**外から見える形**
    (card の fail_head_waiver 欄 + Result + stderr 警告) でだけ免除できる。

    **戻りの dict は「card に書く欄の全体」で、`handoff_path` を必ず含む** (t028)。
    検証を通った handoff は正規化した絶対パス、渡されなかったなら `None` — `None` の欄は
    呼び出し側が card から**消す**。「渡さなければ card に触れない」と読める形にすると、
    `update --status in_progress` で開き直した card に残っていた**古い** handoff_path が、
    head を検証されないまま FAIL の記録に混ざり、dispatcher がそれを読んで Director に
    通知する (Result は `handoff: none` なのに)。card に残る handoff_path を「この報告で
    検証したもの」だけにする、という不変条件を、この関数の戻り値 1 か所に置いてある。
    """
    head = report.get('head')
    no_head = report.get('no_head')
    handoff_path = report.get('handoff_path')
    # card に残す値。検証に使ったのと同一のパス (_handoff_names_head は abspath して読む)。
    # 相対パスはここより前で拒否済みなので、正規化するだけでよい。
    recorded_handoff = os.path.normpath(handoff_path) if handoff_path else None
    usage = ("  plan.sh fail <task_id> [<handoff_path>] --head <sha>\n"
             "  (検証対象が git 管理外: --no-head \"<理由 1 行>\")")
    if head is not None and no_head is not None:
        return "[plan.sh] --head と --no-head は同時に指定できません", {}
    if head is None and no_head is None:
        return ("[plan.sh] fail には検証対象の head (commit SHA) が必要です。\n"
                "古い handoff / 別の head の検証結果を FAIL として再提出できないようにするためです。\n"
                f"{usage}\n"
                "  例: plan.sh fail t001 --head \"$(git rev-parse HEAD)\""), {}
    if handoff_path and not os.path.isabs(handoff_path):
        # dispatcher.sh は相対パスを **registry の親 (main repo)** 基準で読む。ここ (Worker の
        # cwd = worktree) 基準で検証すると、別のファイルを検証して通す / 無いファイルを通す
        # 一方で、dispatcher は main repo の古い handoff を読む — 新設した stale-handoff 確認の
        # 迂回になる。基準を揃えるより、相対パスを受け付けない (--no-head でも同じ)。
        return (f"[plan.sh] handoff_path は絶対パスで渡してください (相対パス: {handoff_path})。\n"
                "dispatcher は相対パスを worktree ではなく main repo 基準で読むため、"
                "検証したファイルと通知に使われるファイルが食い違います。\n"
                "  HANDOFF_PATH=\"$(crewvia_handoff_path \"$AGENT_NAME\" \"$TASK_ID\")\"  # scripts/git-helpers.sh"
                f"\n{usage}"), {}
    if no_head is not None:
        reason = ' '.join(str(no_head).split())
        if not reason:
            return "[plan.sh] --no-head には理由 (空でない 1 行) が必要です", {}
        return None, {'fail_head': None, 'fail_head_waiver': reason,
                      'handoff_path': recorded_handoff}

    full, why = _resolve_head_commit(head)
    if full is None:
        return f"[plan.sh] {why}\n{usage}", {}
    if handoff_path:
        ok, note = _handoff_names_head(handoff_path, full)
        if not ok:
            return f"[plan.sh] {note}\n{usage}", {}
        if note:
            print(f"[plan.sh warn] {note}", file=sys.stderr)
    return None, {'fail_head': full, 'fail_head_waiver': None,
                  'handoff_path': recorded_handoff}


def _gate_terminal_report(kind, meta, report):
    """Worker の結末報告 (done / fail) を検証する **唯一の入口**。

    「done は守るが fail は守らない」形の再発を、入口を 1 つにして防ぐ
    (backlog #8。qa_checkpoints / required_evidence は done だけを守っていた)。
    `meta['status']` を done / failed に書くコマンドは必ずここを通ること —
    `tests/test_fail_evidence.py` が AST で確かめる。

    kind='done': QA Gate → required_evidence (従来どおり。report['result'])
    kind='fail': `_validate_fail_evidence` (別の規則。理由はそこのコメント)

    戻り値: (エラー文 or None, card に書く欄の dict)。呼び出し側が die する。
    """
    if kind == 'done':
        result = report['result']
        task_id = report['task_id']
        # qa_checkpoints が frontmatter にある → ## QA Gate セクション必須
        # qa_checkpoints がない → 後方互換（スキップ）
        has_qa_checkpoints = bool(meta.get('qa_checkpoints'))  # None / [] は False
        qa_fails = _validate_qa_gate(result, has_qa_checkpoints)
        if qa_fails:
            reasons = '\n'.join(qa_fails)
            return (
                f"[plan.sh] QA gate FAIL — 以下のチェックポイントが not_run / failed または欠落しています:\n"
                f"{reasons}\n\n"
                f"plan.sh done をブロックします。\n"
                f"対処方法:\n"
                f"  1. チェックポイントを完遂して再度 plan.sh done を呼ぶ\n"
                f"  2. 完遂できない場合は以下で差し戻す:\n"
                f"     plan.sh needs-director {task_id} \"<理由>\""
            ), {}
        req_ev = meta.get('required_evidence')
        if isinstance(req_ev, list) and req_ev:
            ev_missing = _validate_required_evidence(result, req_ev)
            if ev_missing:
                missing_str = '\n'.join(ev_missing)
                return (
                    f"[plan.sh] required_evidence が result に見つかりません:\n"
                    f"{missing_str}\n\n"
                    f"plan.sh done をブロックします。\n"
                    f"証拠を result に含めてから再度実行するか、\n"
                    f"証拠が存在しない場合は以下で差し戻してください:\n"
                    f"  plan.sh needs-director {task_id} \"<理由>\""
                ), {}
        return None, {}
    if kind == 'fail':
        return _validate_fail_evidence(meta, report)
    raise ValueError(f"unknown terminal report kind: {kind!r}")


# ---------------------------------------------------------------------------
# needs-director command
# ---------------------------------------------------------------------------

def cmd_needs_director(args):
    """plan.sh needs-director <task_id> "<理由>"

    in_progress タスクを needs_director 状態に遷移させる。
    Director の介入を求める。
    - TERMINAL_STATUSES に含まれないため、後続 blocked_by は解除されない
    - Dispatcher は pending 以外の非終端ステータスと同じく割り当て対象外
    - AGENT_NAME の assignment (この task を指すもの) を撤去する (done / fail と同じ)。
      card の `worker` は残るので、dispatcher は判断待ちの Worker を「仕事あり」と読む
    """
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) < 2:
        die("needs-director requires <task_id> and <reason>\\n"
            "Usage: plan.sh needs-director <task_id> \"<理由>\"")
    task_id = positional[0]
    reason = positional[1]

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = [s for s in (state.get('active_missions') or [])
                       if os.path.exists(task_path(s, task_id))]
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('needs-director', task_id, matches)
            else:
                slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status not in ('in_progress',):
            die(f"needs-director requires in_progress task (current: {cur_status})")

        summary, full_text = split_long_freeform(reason)
        meta['status'] = 'needs_director'
        meta['needs_director_reason'] = summary
        if full_text is not None:
            body = body.rstrip() + '\n\n## Needs-Director 詳細\n' + full_text.strip() + '\n'
        save_task(slug, task_id, meta, body)

        # done / fail と同じく、撤去は card の書き換えと同じトランザクションの中で
        # (generation=None の理由も cmd_done を参照)。撤去しないと、codex-review の
        # Kai-codex のように「同時 1 実行」を assignment の有無で判定する側が、終わった
        # run の assignment に恒久的に塞がれる (backlog #13)。判断待ちの Worker が
        # 「仕事なし」と読まれて退役されないことは dispatcher 側が card で見る
        # (`worker_holds_work()`) — assignment を外す前提はそちらに置いてある。
        agent_name = os.environ.get('AGENT_NAME', '')
        if agent_name:
            verdict = retire_assignment(agent_name, slug, task_id, None)
            if verdict not in (ASSIGN_MINE, ASSIGN_ABSENT):
                print(
                    f"[plan.sh warn] {describe_assignment_verdict(agent_name, verdict)}"
                    f" — 削除しませんでした ({agent_name} は別の作業に就いている可能性があります)",
                    file=sys.stderr,
                )

        print(f"[plan.sh] Task {task_id} → needs_director")
        print(f"[plan.sh] Reason: {summary}")
        if full_text is not None:
            print(f"[plan.sh] 全文は task body の '## Needs-Director 詳細' セクションに保存しました。")
        print(f"[plan.sh] Director への通知: plan.sh status で確認してください")

    with_lock(_do)


def cmd_done(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if len(positional) < 2:
        die("done requires <task_id> and <result>")
    task_id = positional[0]
    result = positional[1]
    sync_holder = [None]  # (slug, task_id, result)
    worker_holder = [None]  # worker name for registry bump (None = _do未実行, '' = worker未設定)

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                # Check archived missions to surface a more useful error if the
                # mission was archived between pull and done (race window).
                archived_matches = []
                if os.path.isdir(ARCHIVE_DIR):
                    for entry in os.listdir(ARCHIVE_DIR):
                        archived_task = os.path.join(ARCHIVE_DIR, entry, 'tasks', f"{task_id}.md")
                        if os.path.exists(archived_task):
                            archived_matches.append(entry)
                if archived_matches:
                    die(
                        f"task '{task_id}' exists only in archived mission(s) "
                        f"{archived_matches}. The mission was archived before this "
                        f"done report — re-activate the mission to record the result, "
                        f"or merge the result manually into the archived task file."
                    )
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('done', task_id, matches)
            else:
                slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'failed', 'skipped'):
            die(f"task '{task_id}' is already {cur_status}.")
        if cur_status == 'needs_director':
            reason = meta.get('needs_director_reason', '')
            die(
                f"task '{task_id}' is in needs_director state (理由: {reason})\n"
                f"Director が plan.sh update {task_id} --status in_progress --reset で"
                f" 差し戻してから再度 plan.sh done を呼んでください。"
            )

        # ── QA Gate / required_evidence 検証 (fail と共通の入口) ──────────────
        err, _fields = _gate_terminal_report(
            'done', meta, {'result': result, 'task_id': task_id})
        if err:
            die(err)

        meta['status'] = 'done'
        meta['completed_at'] = now_iso()
        trailing = extract_trailing_body_section(body)
        desc, _ = parse_task_body(body)
        new_body = append_trailing_body_section(build_task_body(desc, result), trailing)
        save_task(slug, task_id, meta, new_body)
        worker_holder[0] = meta.get('worker') or ''  # capture worker for post-lock bump
        sync_holder[0] = (slug, task_id, result)

        # assignment の撤去は card の書き換えと同じトランザクションで行う。
        # generation=None なのは、この経路が「いま card が示している実行」を
        # 同じロックの中で終了させているため (読みと書きの間に隙間が無い)。
        agent_name = os.environ.get('AGENT_NAME', '')
        if agent_name:
            verdict = retire_assignment(agent_name, slug, task_id, None)
            if verdict not in (ASSIGN_MINE, ASSIGN_ABSENT):
                print(
                    f"[plan.sh warn] {describe_assignment_verdict(agent_name, verdict)}"
                    f" — 削除しませんでした ({agent_name} は別の作業に就いている可能性があります)",
                    file=sys.stderr,
                )

        # Mission complete?
        tasks = list_tasks(slug)
        if all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks):
            mission = load_mission(slug)
            mission['status'] = 'done'
            mission['completed_at'] = now_iso()
            save_mission(slug, mission)

        print(f"Done: {slug}/{task_id}")

    with_lock(_do)

    # assignment の撤去は _do() の中 (キューロック内) で完了している。
    # ここから先はキューの外側 — TARGET_DIR と Taskvia の後始末。
    agent_name = os.environ.get('AGENT_NAME', '')

    # Clean up crewvia-worker-{AGENT_NAME}.json from TARGET_DIR (Option D revert)
    # This file is created by start.sh at Worker startup to inject crewvia hooks without
    # modifying the target project's settings.local.json.
    target_dir = os.environ.get('TARGET_DIR', '').strip()
    if target_dir and agent_name:
        worker_settings = os.path.join(target_dir, '.claude', f'crewvia-worker-{agent_name}.json')
        if os.path.exists(worker_settings):
            try:
                os.remove(worker_settings)
                print(f"[plan.sh] crewvia worker settings cleaned up: {worker_settings}")
            except OSError as _e:
                print(f"[plan.sh warn] failed to remove worker settings {worker_settings}: {_e}", file=sys.stderr)

    # Auto-bump task_count in worker registry (Worker Step4 automation)
    # worker_holder[0] is None if _do didn't run, '' if no worker field, else worker name
    if worker_holder[0]:
        # Use CREWVIA_REPO_ROOT when available so that Workers calling plan.sh done
        # from a worktree still update the main repo's registry (not the worktree's).
        _actual_root = os.environ.get('CREWVIA_REPO_ROOT', REPO_ROOT)
        _registry = os.path.join(_actual_root, 'registry', 'workers.yaml')
        _lib = os.path.join(_actual_root, 'scripts', 'lib_registry.py')
        try:
            _result = subprocess.run(
                [sys.executable, _lib, 'bump-task-count', _registry, worker_holder[0]],
                check=True, capture_output=True, text=True
            )
            if _result.stdout.strip() == 'no-op':
                print(
                    f"[plan.sh warn] worker '{worker_holder[0]}' not found in registry"
                    f" — bump-task-count skipped (task '{task_id}')",
                    file=sys.stderr
                )
        except Exception as _e:
            print(
                f"[plan.sh warn] bump-task-count failed for '{worker_holder[0]}': {_e}",
                file=sys.stderr
            )
    elif worker_holder[0] is not None:
        # _do completed but task had no 'worker' field
        print(
            f"[plan.sh warn] task '{task_id}' has no 'worker' field — skipping bump-task-count",
            file=sys.stderr
        )

    if sync_holder[0]:
        ok = taskvia_sync_done(*sync_holder[0])
        _print_sync_summary(ok)


def cmd_fail(args):
    """plan.sh fail <task_id> [<handoff_path>] (--head <sha> | --no-head <理由>) [--mission <slug>]

    検証対象の head (commit SHA) を必須にする。証拠の要求と設計判断は
    `_gate_terminal_report` / `_validate_fail_evidence` を参照 (done と同じ入口を通る)。
    """
    opts, positional = parse_opts(args, {
        '--mission': 'value', '--head': 'value', '--no-head': 'value',
    })
    if not positional:
        die("fail requires <task_id>")
    task_id = positional[0]
    handoff_path = positional[1] if len(positional) > 1 else None
    knowledge_info = [None]  # populated inside _do if rework limit reached

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('fail', task_id, matches)
            else:
                slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'failed', 'skipped'):
            die(f"task '{task_id}' is already {cur_status}.")

        # ── 証拠の検証 (done と共通の入口。card は 1 バイトも書く前) ─────────────
        err, fields = _gate_terminal_report('fail', meta, {
            'head': opts.get('--head'),
            'no_head': opts.get('--no-head'),
            'handoff_path': handoff_path,
        })
        if err:
            die(err)

        # card に書く証拠の欄は fields が全部持っている。None は「この報告には無い」の意味で、
        # card から**消す** (t028)。handoff_path も同じ: 渡されなかったのに card の古い値を
        # 残すと、開き直し (`update --status in_progress` 等) で持ち越された別 head 時点の
        # handoff が検証されないまま failed の card に載り、dispatcher が読んで通知する。
        # 消す側 (ここ) を選んだのは、開き直す経路 (update --reset / --status / 手書き / 旧版の
        # card) の全部を塞ぐより、failed に至る唯一の入口で塞ぐ方が漏れないため。
        # 開き直す側の掃除は cmd_update が別に行う (card を failed 以外の間も綺麗に保つ)。
        recorded_handoff = fields.get('handoff_path')
        meta['status'] = 'failed'
        meta['completed_at'] = now_iso()
        for key, val in fields.items():
            if val is None:
                meta.pop(key, None)     # 前回の FAIL の証拠を持ち越さない
            else:
                meta[key] = val
        if fields.get('fail_head'):
            evidence = f"head: {fields['fail_head']}"
        else:
            evidence = f"head: none (--no-head: {fields['fail_head_waiver']})"
        trailing = extract_trailing_body_section(body)
        desc, _ = parse_task_body(body)
        new_body = append_trailing_body_section(
            build_task_body(
                desc, f"FAILED — {evidence} — handoff: {recorded_handoff or 'none'}"),
            trailing
        )
        save_task(slug, task_id, meta, new_body)
        print(f"Failed: {slug}/{task_id}")
        if fields.get('fail_head_waiver'):
            print(
                f"[plan.sh warn] --no-head で head なしの FAIL を記録しました"
                f" (理由: {fields['fail_head_waiver']}) — Director は検証対象を確認してください",
                file=sys.stderr,
            )

        # done と同じく、撤去は card の書き換えと同じトランザクションの中で。
        agent_name = os.environ.get('AGENT_NAME', '')
        if agent_name:
            verdict = retire_assignment(agent_name, slug, task_id, None)
            if verdict not in (ASSIGN_MINE, ASSIGN_ABSENT):
                print(
                    f"[plan.sh warn] {describe_assignment_verdict(agent_name, verdict)}"
                    f" — 削除しませんでした ({agent_name} は別の作業に就いている可能性があります)",
                    file=sys.stderr,
                )

        # Rework learning loop: record in knowledge/director.md if rework limit was reached
        rework = meta.get('rework_count') or 0
        max_rework = meta.get('max_rework') or 3
        if rework >= max_rework:
            knowledge_info[0] = (task_id, slug, rework, max_rework, recorded_handoff)

    with_lock(_do)

    # Post-lock: write rework pattern to knowledge/director.md
    if knowledge_info[0]:
        _append_knowledge_director(*knowledge_info[0])


def cmd_status(args):
    opts, _ = parse_opts(args, {'--mission': 'value', '--all': 'bool'})
    state = load_state()

    if opts.get('--mission'):
        _print_mission_detail(opts['--mission'])
        return

    slugs = list(state.get('active_missions') or [])
    archived_slugs = []
    if opts.get('--all') and os.path.exists(ARCHIVE_DIR):
        for entry in sorted(os.listdir(ARCHIVE_DIR)):
            full = os.path.join(ARCHIVE_DIR, entry)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, 'mission.yaml')):
                archived_slugs.append(entry)

    if not slugs and not archived_slugs:
        print("No active missions.")
        return

    if slugs:
        print(f"Active missions ({len(slugs)}):")
        print()
        for slug in slugs:
            _print_mission_summary(slug, archived=False)
            print()

    if archived_slugs:
        print(f"Archived missions ({len(archived_slugs)}):")
        print()
        for slug in archived_slugs:
            _print_mission_summary(slug, archived=True)
            print()


def _resolve_mission_base(slug):
    if os.path.exists(mission_dir(slug)):
        return mission_dir(slug), False
    archived = os.path.join(ARCHIVE_DIR, slug)
    if os.path.exists(archived):
        return archived, True
    return None, False


def _print_mission_summary(slug, archived=False):
    base = os.path.join(ARCHIVE_DIR, slug) if archived else mission_dir(slug)
    mission_path = os.path.join(base, 'mission.yaml')
    if not os.path.exists(mission_path):
        print(f"  {slug} — (mission.yaml missing)")
        return
    text, problem = try_read_queue_file(mission_path)
    if problem is not None:
        # 1 つの mission を読めなかっただけで、残りを画面から消さない。
        print(f"  {slug} — (mission.yaml unreadable: {problem})")
        return
    try:
        mission = parse_yaml(text, source=mission_path)
    except ValueError as e:
        print(f"  {slug} — (mission.yaml unparseable: {e})")
        return
    tasks = list_tasks(slug, base_dir=os.path.join(base, 'tasks'))
    total = len(tasks)
    done = sum(1 for (m, _) in tasks if m.get('status') == 'done')
    in_prog = [(m, b) for (m, b) in tasks if m.get('status') == 'in_progress']
    needs_dir = [(m, b) for (m, b) in tasks if m.get('status') == 'needs_director']
    corrupted = [(m, b) for (m, b) in tasks if m.get('status') == CORRUPT_TASK_STATUS]
    done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
    task_statuses = {m['id']: m.get('status') for (m, _) in tasks}
    held = []
    for (m, _) in tasks:
        if m.get('status') != 'pending':
            continue
        verdict = card_dependencies(m, done_ids, task_statuses)
        if verdict.held:
            held.append((m, verdict.held))

    title = mission.get('title', '(unnamed)')
    status = mission.get('status', 'in_progress')
    marker = '[archived] ' if archived else ''
    print(f"  {marker}{slug} — {title}")
    print(f"    Status: {status}  Progress: {done}/{total}")
    for (m, _) in in_prog:
        worker = m.get('worker') or '?'
        print(f"    🔄 {m['id']} {m['title']} ({worker})")
    for (m, _) in needs_dir:
        worker = m.get('worker') or '?'
        reason = m.get('needs_director_reason', '')
        reason_str = f" — {reason}" if reason else ''
        print(f"    🆘 {m['id']} {m['title']} ({worker}){reason_str}")
    for (m, _) in corrupted:
        err = m.get('parse_error', '')
        err_str = f" — {err}" if err else ''
        print(f"    💥 {m['id']} [破損]{err_str}")
    for (m, deps) in held:
        # 既定の status (要約) にも出す。保留は誰も自動では解かないので、
        # 詳細を開かないと見えない場所に置くと「永久保留」になる。
        print(f"    🛑 {m['id']} {m['title']} — {held_dependency_hint(m['id'], deps, slug)}")


def _print_mission_detail(slug):
    base, archived = _resolve_mission_base(slug)
    if not base:
        die(f"mission '{slug}' not found.")
    mission_path = os.path.join(base, 'mission.yaml')
    text = read_queue_file(mission_path, 'mission file')
    try:
        mission = parse_yaml(text, source=mission_path)
    except ValueError as e:
        die(f"failed to parse {mission_path}: {e}")
    tasks = list_tasks(slug, base_dir=os.path.join(base, 'tasks'))

    print(f"Mission: {mission.get('title', '(unnamed)')}")
    print(f"Slug:    {slug}{' [archived]' if archived else ''}")
    print(f"Status:  {mission.get('status', 'in_progress')}")
    print()

    done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
    task_statuses = {m['id']: m.get('status') for (m, _) in tasks}
    held_lines = []
    for (m, _) in tasks:
        st = m.get('status', 'pending')
        icon = STATUS_ICON.get(st, '❓')
        tid = m['id']
        title = m['title']
        bb = m.get('blocked_by') or []
        if st in ('done', 'verified'):
            worker = m.get('worker') or ''
            suffix = f"({worker}, 完了)" if worker else "(完了)"
        elif st == 'in_progress':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 進行中)" if worker else "(進行中)"
        elif st == 'ready_for_verification':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 検証待ち)" if worker else "(検証待ち)"
        elif st == 'verifying':
            suffix = "(検証中)"
        elif st == 'verification_failed':
            suffix = "(検証失敗)"
        elif st == 'needs_human_review':
            suffix = "(要人間レビュー)"
        elif st == 'skipped':
            suffix = "(スキップ)"
        elif st == 'needs_director':
            worker = m.get('worker') or ''
            reason = m.get('needs_director_reason', '')
            reason_str = f" — {reason}" if reason else ''
            suffix = f"({worker}, 要Director){reason_str}" if worker else f"(要Director){reason_str}"
        elif st == 'failed':
            worker = m.get('worker') or ''
            suffix = f"({worker}, 失敗)" if worker else "(失敗)"
        elif st == 'blocked':
            reason = m.get('blocked_reason', '')
            reason_str = f" — {reason}" if reason else ''
            suffix = f"(ブロック中){reason_str}"
        elif st == CORRUPT_TASK_STATUS:
            err = m.get('parse_error', '')
            err_str = f" — {err}" if err else ''
            suffix = f"(破損 — 手動修復が必要){err_str}"
        elif bb:
            # 表示も pull / dispatcher と同じ規則から (別に数えると、進める
            # task が blocked と出る / 保留が waiting に見える、という食い違いになる)。
            verdict = card_dependencies(m, done_ids, task_statuses)
            if verdict.held:
                suffix = f"(HELD: {', '.join(verdict.held)} が failed — Director の判断待ち)"
                held_lines.append(held_dependency_hint(tid, verdict.held, slug))
            elif verdict.unmet:
                suffix = f"(blocked: {', '.join(verdict.unmet)})"
            else:
                suffix = "(pending)"
        else:
            suffix = "(pending)"
        timeout_suffix = ''
        to = m.get('timeout')
        if isinstance(to, dict):
            parts = []
            if 'idle' in to:
                parts.append(f"idle={to['idle']}s")
            if 'max' in to:
                parts.append(f"max={to['max']}s")
            if parts:
                timeout_suffix = f" [{' '.join(parts)}]"
        print(f"[{icon}] {tid} {title} {suffix}{timeout_suffix}")

    total = len(tasks)
    done_count = sum(1 for (m, _) in tasks if m.get('status') == 'done')
    print()
    print(f"Progress: {done_count}/{total} done")
    if held_lines:
        print()
        print("Held (failed の依存 — 自動では進まない):")
        for line in held_lines:
            print(f"  {line}")


def cmd_ready_for_verification(args):
    opts, positional = parse_opts(args, {'--mission': 'value'})
    if not positional:
        die("ready-for-verification requires <task_id>")
    task_id = positional[0]

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('ready-for-verification', task_id, matches)
            else:
                slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status != 'in_progress':
            die(
                f"ready-for-verification requires task to be in_progress, "
                f"but '{task_id}' is currently '{cur_status}'."
            )

        meta['status'] = 'ready_for_verification'
        save_task(slug, task_id, meta, body)
        print(f"Ready for verification: {slug}/{task_id}")

    with_lock(_do)


def cmd_verify_result(args):
    """
    Usage: plan.sh verify-result <task_id> <verdict> [--notes "..."] [--mission <slug>]
    verdict: pass | fail | needs_human_review

    Appends a verification entry to the ## Verification section of the task file.
    pass          → status: verified (terminal)
    fail          → rework_count += 1, status: in_progress (or needs_human_review if max_rework exceeded)
    needs_human_review → status: needs_human_review
    """
    opts, positional = parse_opts(args, {'--mission': 'value', '--notes': 'value'})
    if len(positional) < 2:
        die("verify-result requires <task_id> <verdict>")
    task_id = positional[0]
    verdict = positional[1]
    VALID_VERDICTS = {'pass', 'fail', 'needs_human_review'}
    if verdict not in VALID_VERDICTS:
        die(f"verdict must be one of: {', '.join(sorted(VALID_VERDICTS))}")
    notes = opts.get('--notes', '')

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = []
            for s in state.get('active_missions') or []:
                if os.path.exists(task_path(s, task_id)):
                    matches.append(s)
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('verify-result', task_id, matches)
            else:
                slug = matches[0]

        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)
        cur_status = meta.get('status')
        if cur_status in ('done', 'verified', 'skipped', 'failed'):
            die(f"task '{task_id}' is already {cur_status}.")

        # Build and append verification entry
        timestamp = now_iso()
        entry_lines = [f"\n### {timestamp}", f"**Verdict:** {verdict}"]
        if notes:
            entry_lines.append(f"**Notes:** {notes}")
        verification_entry = '\n'.join(entry_lines) + '\n'

        if '## Verification' in body:
            body_new = body.rstrip() + '\n' + verification_entry
        else:
            body_new = body.rstrip() + '\n\n## Verification\n' + verification_entry

        # Status transition
        if verdict == 'pass':
            meta['status'] = 'verified'
            meta['completed_at'] = now_iso()
        elif verdict == 'fail':
            rework = (meta.get('rework_count') or 0) + 1
            max_rework = meta.get('max_rework') or 3
            meta['rework_count'] = rework
            if rework >= max_rework:
                meta['status'] = 'needs_human_review'
                print(
                    f"rework_count ({rework}) >= max_rework ({max_rework}): "
                    f"escalating to needs_human_review"
                )
            else:
                meta['status'] = 'in_progress'
        else:  # needs_human_review
            meta['status'] = 'needs_human_review'

        save_task(slug, task_id, meta, body_new)

        # Check mission completion (only on pass→verified)
        if verdict == 'pass':
            tasks = list_tasks(slug)
            if all(m.get('status') in TERMINAL_STATUSES for (m, _) in tasks):
                mission = load_mission(slug)
                mission['status'] = 'done'
                mission['completed_at'] = now_iso()
                save_mission(slug, mission)

        print(f"verify-result: {slug}/{task_id} → {meta['status']} (verdict={verdict})")

    with_lock(_do)


_MODE_ORDER = ['light', 'standard', 'strict']


def upgrade_mode(current, proposed):
    """Return the stricter of two verification modes (light < standard < strict)."""
    ci = _MODE_ORDER.index(current) if current in _MODE_ORDER else 0
    pi = _MODE_ORDER.index(proposed) if proposed in _MODE_ORDER else 0
    return _MODE_ORDER[max(ci, pi)]


def _apply_risk_flags(slug, plan_review_path):
    """Parse ## Risk Flags from plan_review.md and upgrade task verification.mode."""
    content, problem = try_read_queue_file(plan_review_path)
    if problem is not None:
        return

    # Find ## Risk Flags section
    in_risk_flags = False
    current_task_id = None
    recommended_mode = None
    upgrades: list = []  # list of (task_id, mode)

    for line in content.splitlines():
        if re.match(r'^##\s+Risk\s+Flags', line, re.IGNORECASE):
            in_risk_flags = True
            continue
        if in_risk_flags:
            if re.match(r'^##', line):
                break  # Next section — stop
            m_task = re.match(r'^-\s+task:\s*(\S+)', line)
            if m_task:
                if current_task_id and recommended_mode:
                    upgrades.append((current_task_id, recommended_mode))
                current_task_id = m_task.group(1).strip()
                recommended_mode = None
                continue
            m_mode = re.match(r'\s+recommended_mode:\s*(\S+)', line)
            if m_mode and current_task_id:
                recommended_mode = m_mode.group(1).strip()

    if current_task_id and recommended_mode:
        upgrades.append((current_task_id, recommended_mode))

    if not upgrades:
        return

    # Apply upgrades to task files
    for task_id, proposed_mode in upgrades:
        task_file = None
        for fn in os.listdir(os.path.join(MISSIONS_DIR, slug, 'tasks')):
            if fn == f"{task_id}.md":
                task_file = os.path.join(MISSIONS_DIR, slug, 'tasks', fn)
                break
        if not task_file or not os.path.exists(task_file):
            print(f"[risk-flags] task '{task_id}' not found in mission '{slug}' — skipping", file=sys.stderr)
            continue

        meta, body = load_task(slug, task_id)
        verification = meta.get('verification') or {}
        if not isinstance(verification, dict):
            verification = {}
        current_mode = verification.get('mode') or 'standard'
        new_mode = upgrade_mode(current_mode, proposed_mode)
        if new_mode != current_mode:
            verification['mode'] = new_mode
            meta['verification'] = verification
            save_task(slug, task_id, meta, body)
            print(
                f"[risk-flags] task '{task_id}': verification.mode {current_mode} → {new_mode} "
                f"(recommended_mode={proposed_mode})"
            )
        else:
            print(
                f"[risk-flags] task '{task_id}': verification.mode stays {current_mode} "
                f"(already >= recommended {proposed_mode})"
            )


def _append_knowledge_director(task_id, slug, rework_count, max_rework, handoff_path=None):
    """Append a rework-limit record to knowledge/director.md (create with header if absent)."""
    repo_root = os.path.dirname(QUEUE_DIR)
    knowledge_dir = os.path.join(repo_root, 'knowledge')
    os.makedirs(knowledge_dir, exist_ok=True)
    knowledge_path = os.path.join(knowledge_dir, 'director.md')
    header = (
        "# Director Knowledge Base\n\n"
        "Director が過去のミッション実績から学んだパターンを自動追記するファイル。\n"
        "計画精度改善のために参照すること。\n"
    )
    timestamp = now_iso()
    entry = (
        f"\n## {timestamp} — rework 上限到達: {task_id}\n"
        f"- mission: {slug}\n"
        f"- rework_count: {rework_count} / max_rework: {max_rework}\n"
        f"- handoff_path: {handoff_path or 'none'}\n"
    )
    try:
        if not os.path.exists(knowledge_path):
            with open(knowledge_path, 'w') as f:
                f.write(header)
        with open(knowledge_path, 'a') as f:
            f.write(entry)
        print(
            f"[knowledge] appended rework pattern to knowledge/director.md "
            f"(task={task_id}, rework={rework_count}/{max_rework})"
        )
    except OSError as e:
        print(f"WARNING: failed to write knowledge/director.md: {e}", file=sys.stderr)


def _load_lint_module():
    """Load lint_plan.py dynamically (same pattern as cmd_lint)."""
    import importlib.util, pathlib
    repo_root = os.path.dirname(QUEUE_DIR)
    lint_path = pathlib.Path(repo_root) / 'scripts' / 'lint_plan.py'
    spec = importlib.util.spec_from_file_location('lint_plan', lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, os.path.join(repo_root, 'config')


def _load_verdict_module():
    """Load lib_verdict.py dynamically, lazily, from inside cmd_review only.

    t010 (QA t008 FINDING-1/2/3): plan_review.md の verdict 抽出は
    scripts/lib_verdict.py に一本化した (wait_for_plan_review.sh も同じ
    関数を使う)。F1 (t002, mission 20260912-verdict-ci-launcher):
    以前はここで判定できなかった場合に normalize_plan_review_verdict.py が
    ファイル全体を走査して別表記を救済していたが、その経路が不変条件
    (判定は1点だけを完全一致で読む) を迂回する唯一の抜け道になっていたため
    削除した。別表記の救済は scripts/review-plan.sh の構造化出力経路
    (`claude --json-schema`) に一本化されている。

    重要: これをモジュールのトップレベルで `import` すると、review 以外の
    全サブコマンド (pull/done/needs-director 等) が、lib_verdict.py を
    持たない fixture 経由で plan.sh を実行するテスト (例:
    scripts/test_kai_review.sh は scripts/plan.sh だけをコピーした
    FIXTURE_REPO で `plan.sh needs-director` 等を呼ぶ) まで巻き込んで
    ImportError で壊してしまう。cmd_review の中でだけ、必要になった時点で
    遅延 import する (_load_lint_module と同じ「per-command 動的ロード」の
    考え方)。REPO_ROOT (sys.argv[3]) を使う — QUEUE_DIR ベースの repo_root
    (テストで scratch に差し替え可能、review-plan.sh のスタブ差し替えに使う
    ものと同じ) ではなく、常に「実際に起動された plan.sh 自身」の
    scripts/ ディレクトリから読む。stub 差し替えの対象ではない、判定
    ロジックの本体だからである。
    """
    import importlib.util, pathlib
    verdict_path = pathlib.Path(REPO_ROOT) / 'scripts' / 'lib_verdict.py'
    spec = importlib.util.spec_from_file_location('lib_verdict', verdict_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_review(args):
    """
    Usage: plan.sh review <slug>

    1. Runs lint as a pre-step (FAIL → treated as revise, exit 1)
    2. Checks max_review_cycles
    3. Sets mission status to reviewing, increments cycle_count
    4. Invokes scripts/review-plan.sh <slug> (with CREWVIA_PLAN_REVIEW_RUN_ID)
    5. Reads the verdict review-plan.sh bound to plan_review.verdict for this
       run (never re-reads plan_review.md) and updates mission:
       - approve → status: ready
       - revise  → status: drafting, cycle_count++
       - reject  → status: drafting, cycle_count++
    """
    opts, positional = parse_opts(args, {})
    if not positional:
        die("review requires <mission_slug>")
    slug = positional[0]

    # --- Step 1: lint pre-check ---
    print(f"[review] running lint on '{slug}'...", file=sys.stderr)
    try:
        lint_mod, config_dir = _load_lint_module()
        lint_rc = lint_mod.lint_mission(slug, QUEUE_DIR, config_dir, strict=False)
    except Exception as e:
        die(f"lint failed to load: {e}")
    if lint_rc != 0:
        print(
            f"[review] lint FAIL — treating as revise. Fix lint errors before review.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"[review] lint OK", file=sys.stderr)

    # --- Step 2: check mission status + cycle limit ---
    def _do_start():
        mission = load_mission(slug)
        cur_status = mission.get('status')
        allowed = ('drafting', 'ready')
        if cur_status not in allowed:
            die(
                f"mission '{slug}' status is '{cur_status}' — "
                f"only {allowed} missions can enter review"
            )
        review = mission.get('review') or {}
        if not isinstance(review, dict):
            review = {}
        cycle_count = review.get('cycle_count') or 0
        max_cycles = mission.get('max_review_cycles') or 3
        if cycle_count >= max_cycles:
            print(
                f"[review] ESCALATION: '{slug}' has reached max_review_cycles ({max_cycles}). "
                f"Director intervention required.",
                file=sys.stderr,
            )
            sys.exit(1)

        review['cycle_count'] = cycle_count + 1
        review['reviewed_at'] = now_iso()
        mission['review'] = review
        mission['status'] = 'reviewing'
        save_mission(slug, mission)
        print(f"[review] mission '{slug}' → reviewing (cycle {cycle_count + 1}/{max_cycles})")

    with_lock(_do_start)

    # --- Step 3: invoke review-plan.sh ---
    import subprocess
    import secrets
    repo_root = os.path.dirname(QUEUE_DIR)
    review_script = os.path.join(repo_root, 'scripts', 'review-plan.sh')
    verdict_file = os.path.join(MISSIONS_DIR, slug, 'plan_review.verdict')

    # t018 (mission 20260912-verdict-ci-launcher, QA t016 Finn E_A3): plan.sh は
    # plan_review.verdict が「今回の review-plan.sh 実行で書かれた」ことを
    # 自分で確認する。t015 時点では review-plan.sh 冒頭の `rm -f` だけが
    # 前 cycle の古い approve を消す唯一の仕組みで、rm も書き込みもせずに
    # exit 0 する review-plan.sh (差し替え・将来の改修ミス) があれば古い
    # approve がそのまま消費されていた。
    #   1. 実行ごとの識別子 (run_id) を作って環境変数で渡し、review-plan.sh は
    #      verdict と一緒に書く。Step 4 で一致しなければ fail-closed。
    #   2. 呼び出し前に自分でも古いファイルを消す (多重防御。消せなくても
    #      1 の照合で弾かれる)。
    review_run_id = secrets.token_hex(16)
    try:
        os.remove(verdict_file)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(
            f"[review] WARNING: could not remove stale {verdict_file} ({e}); "
            f"the run id check still rejects it",
            file=sys.stderr,
        )
    review_env = dict(os.environ)
    review_env['CREWVIA_PLAN_REVIEW_RUN_ID'] = review_run_id

    print(f"[review] invoking review-plan.sh...", file=sys.stderr)
    proc = subprocess.run(['bash', review_script, slug], env=review_env)

    def _rollback_to_drafting(reason, refund_cycle=False):
        """P2: rollback mission from reviewing → drafting on failure.

        refund_cycle=True (t011, QA t009 FINDING-B): Step 2 (_do_start) が
        review-plan.sh 呼び出し前に cycle_count を先食いしているため、判定不能
        (reviewer の書式ミスで verdict が読めなかった) で打ち切った場合は
        その先食いを元に戻す。reviewer の書式ミスで Director が review cycle を
        失うのは筋が悪いため。
        """
        def _do_rollback():
            m = load_mission(slug)
            if m.get('status') == 'reviewing':
                m['status'] = 'drafting'
                if refund_cycle:
                    review = m.get('review') or {}
                    if not isinstance(review, dict):
                        review = {}
                    cc = review.get('cycle_count') or 0
                    if cc > 0:
                        review['cycle_count'] = cc - 1
                    m['review'] = review
                save_mission(slug, m)
                print(f"[review] rollback: mission '{slug}' → drafting ({reason})", file=sys.stderr)
        with_lock(_do_rollback)

    review_output = os.path.join(MISSIONS_DIR, slug, 'plan_review.md')

    if proc.returncode != 0:
        # t002: plan_review.md 自体は書かれているのにフォーマットだけが原因で
        # review-plan.sh がタイムアウトすることがある (別表記の verdict が
        # 構造化出力経路でも判定できなかったケース)。この場合
        # 判定内容自体は活かせる可能性が高く、cycle_count を無駄にもう1回消費
        # させるより Director に手動確認を促す方が安全側。
        if os.path.exists(review_output):
            _rollback_to_drafting(
                "review-plan.sh timed out but plan_review.md exists",
                refund_cycle=True,
            )
            die(
                f"review-plan.sh failed or timed out for mission '{slug}', but "
                f"{review_output} was written — inspect it by hand before re-running "
                f"review. If the verdict itself is legible but just in a non-standard "
                f"format, fix the format manually instead of consuming another review cycle."
            )
        _rollback_to_drafting("review-plan.sh failed")
        die(f"review-plan.sh failed or timed out for mission '{slug}'")

    # --- Step 4: read the verdict review-plan.sh bound to plan_review.verdict ---
    # t015 (mission 20260912-verdict-ci-launcher, Director 設計判断1 — QA t003
    # Finn FAIL-1 実測 / Kai-codex P1 TOCTOU への対応): plan.sh はもう
    # plan_review.md を独立に読み直さない。
    #
    # 旧実装 (t010〜t012) はここで scripts/lib_verdict.py の
    # extract_canonical_verdict() を plan_review.md に対して**独立に**再実行
    # していた。review-plan.sh 側で FINAL_VERDICT が確定してから
    # (mux_kill による) reviewer プロセスの終了までの間に plan_review.md の
    # 1行目が書き換わると、review-plan.sh が検証した値と plan.sh がここで
    # 読む値がずれてしまう — QA t003 (Finn) が「prose=revise で検証 →
    # mux_kill 中に reviewer が1行目を approve に再 Write、structured 出力の
    # 確認なしに approve が消費される」ことを decisive に実測した (T1/T1r)。
    #
    # 修正: review-plan.sh が確定した verdict だけを、review-plan.sh 以外の
    # 誰にも (plan-reviewer セッションにも mux_kill にも) 書き換えられない
    # 専用ファイル queue/missions/<slug>/plan_review.verdict に書かせ、
    # plan.sh はそれだけを消費する。plan_review.md はもう判定には使わない
    # (人間向けの記録としては残る)。
    if not os.path.exists(verdict_file):
        _rollback_to_drafting("no valid verdict (plan_review.verdict not found)", refund_cycle=True)
        die(
            f"No valid verdict found for mission '{slug}' — review-plan.sh did not "
            f"produce {verdict_file}. If {review_output} exists, inspect it by hand "
            f"(review-plan.sh may have refused to confirm a verdict without structured "
            f"output; see its stderr log for the reason)."
        )

    # newline='' (F2/FAIL-2 と同じ理由): 万一ファイルが破損していても改行
    # 変換で誤って読み取らないようにする。
    #
    # t018 (鮮度): 形式は 2 行ちょうど "<verdict>\nrun_id=<Step 3 で渡した識別子>\n"。
    # 識別子が一致しない (前 cycle の残骸・別の実行が書いたもの・識別子を知らない
    # 旧形式の 1 行ファイル) なら、値が正しくても消費せず fail-closed。
    # 読めない / UTF-8 として解釈できない場合も、mission を reviewing のまま
    # 残さないよう rollback してから止める。
    # 種類のガードも通す (t018)。`newline=''` は下の「2 行ちょうど」の検査が
    # 改行変換を前提にしていないため必須なので、ガード側にそのまま渡す。
    verdict_raw, problem = try_read_queue_file(verdict_file, newline='')
    if problem is not None:
        _rollback_to_drafting("plan_review.verdict is unreadable", refund_cycle=True)
        die(f"could not read {verdict_file} ({problem}) — refusing to guess (fail-closed).")
    verdict_lines = verdict_raw.split('\n')
    if (
        len(verdict_lines) != 3
        or verdict_lines[1] != f'run_id={review_run_id}'
        or verdict_lines[2] != ''
    ):
        _rollback_to_drafting(
            "plan_review.verdict was not written by this review run", refund_cycle=True
        )
        die(
            f"plan_review.verdict for mission '{slug}' was not written by this review run "
            f"(expected '<verdict>\\nrun_id={review_run_id}\\n', got {verdict_raw[:200]!r}) "
            f"— refusing a stale or foreign verdict (fail-closed)."
        )
    verdict = verdict_lines[0]

    # 防御的 allowlist チェック (review-plan.sh は既に enum 適合を検証済みの
    # 値しか書かないはずだが、ファイル破損・部分書き込み等に備えて plan.sh
    # 側でも独立に値そのものを検証する — plan_review.md の書式解析はしない、
    # 値の完全一致だけを見る「判定 unit を1つに絞る」原則の延長)。
    verdict_mod = _load_verdict_module()
    if verdict not in verdict_mod.CANONICAL_VERDICTS:
        _rollback_to_drafting("plan_review.verdict has an invalid value", refund_cycle=True)
        die(
            f"plan_review.verdict for mission '{slug}' has an unexpected value "
            f"({verdict_raw!r}) — refusing to guess (fail-closed)."
        )

    # --- Step 5: update mission based on verdict ---
    def _do_verdict():
        mission = load_mission(slug)
        review = mission.get('review') or {}
        if not isinstance(review, dict):
            review = {}
        review['last_verdict'] = verdict
        review['reviewed_at'] = now_iso()
        mission['review'] = review
        if verdict == 'approve':
            mission['status'] = 'ready'
            print(f"[review] verdict: approve → mission '{slug}' is now ready for launch")
        else:
            mission['status'] = 'drafting'
            print(
                f"[review] verdict: {verdict} → mission '{slug}' returned to drafting "
                f"(cycle {review.get('cycle_count', '?')} of {mission.get('max_review_cycles', 3)})"
            )
        save_mission(slug, mission)

    with_lock(_do_verdict)

    # --- Step 6: apply risk_flags → verification.mode upgrade ---
    _apply_risk_flags(slug, review_output)


def cmd_launch(args):
    """
    Usage: plan.sh launch <slug>

    Transitions mission from status: ready → in_progress, enabling workers to pull tasks.
    Rejects with an error if status is not ready (drafting/reviewing require plan.sh review first).
    """
    opts, positional = parse_opts(args, {})
    if not positional:
        die("launch requires <mission_slug>")
    slug = positional[0]

    def _do():
        mission = load_mission(slug)
        cur_status = mission.get('status')
        if cur_status != 'ready':
            die(
                f"mission '{slug}' status is '{cur_status}' — only 'ready' missions can be launched. "
                f"Run 'plan.sh review {slug}' first to get reviewer approval."
            )
        mission['status'] = 'in_progress'
        save_mission(slug, mission)
        print(f"Launched: '{slug}' is now in_progress — workers can pull tasks")

    with_lock(_do)


def cmd_task_graph(args):
    """Usage: plan.sh task-graph

    herdr-task-graph 用の tasks.json を今すぐ書き出してパスを印字する。
    queue は変更しない。普段は queue を書き換えるサブコマンドが自動で呼ぶので、
    これを使うのは初回のブートストラップと、生成結果を目で見たいときだけ。

    自動経路 (maybe_refresh_task_graph) と **同じ foreign-queue ガードを掛ける**。
    手動だけ素通りさせると、`CREWVIA_QUEUE` を付け替えて走る隔離 QA が 1 回
    叩くだけで、Director が見ているグラフがテスト用の queue の中身に化ける。
    自動経路と違って黙って引き返さず失敗させるのは、こちらは人が「今すぐ
    書き出せ」と言った実行だからで、何も起きないことの方が分かりにくい。
    """
    parse_opts(args, {})
    if not task_graph_enabled():
        print(
            "[plan.sh] CREWVIA_TASK_GRAPH=0 のため生成しません",
            file=sys.stderr,
        )
        return
    if not task_graph_queue_matches_root():
        die(
            f"[plan.sh] task-graph: CREWVIA_QUEUE ({QUEUE_DIR}) が "
            f"{task_graph_repo_root()}/queue ではありません。"
            f"このまま生成すると本体の生成物をこの queue の中身で上書きします。\n"
            f"  hint: この queue のグラフを見たいなら書き先を明示してください "
            f"(CREWVIA_TASK_GRAPH_FILE=<path> plan.sh task-graph)",
            PRECONDITION_UNMET,
        )
    print(refresh_task_graph())


def cmd_lint(args):
    opts, positional = parse_opts(args, {'--strict': 'bool', '--mission': 'value'})
    slug = opts.get('--mission') or (positional[0] if positional else None)
    if not slug:
        state = load_state()
        active = state.get('active_missions') or []
        if not active:
            die("lint: no active missions and no --mission specified")
        slug = active[0]
    strict = bool(opts.get('--strict'))
    import importlib.util, pathlib
    repo_root = os.path.dirname(QUEUE_DIR)
    lint_path = pathlib.Path(repo_root) / 'scripts' / 'lint_plan.py'
    spec = importlib.util.spec_from_file_location('lint_plan', lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    config_dir = os.path.join(repo_root, 'config')
    rc = mod.lint_mission(slug, QUEUE_DIR, config_dir, strict=strict)
    sys.exit(rc)


def cmd_archive(args):
    opts, positional = parse_opts(args, {})
    if not positional:
        die("archive requires <slug>")
    slug = positional[0]

    def _do():
        state = load_state()
        src = mission_dir(slug)
        if not os.path.exists(src):
            die(f"mission '{slug}' not found.")
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        dst = os.path.join(ARCHIVE_DIR, slug)
        if os.path.exists(dst):
            die(f"archive target already exists: {dst}")
        shutil.move(src, dst)

        active = state.get('active_missions') or []
        if slug in active:
            active.remove(slug)
        state['active_missions'] = active
        if state.get('default_mission') == slug:
            state['default_mission'] = active[0] if active else None
        save_state(state)
        print(f"Archived: {slug} → archive/{slug}")

    with_lock(_do)
    ok = taskvia_sync_archive(slug)
    _print_sync_summary(ok)


def _resync_one(slug):
    """Push one local mission + all its tasks to Taskvia (upsert, idempotent)."""
    if not os.path.exists(mission_dir(slug)):
        print(f"[resync] WARNING: mission '{slug}' not found locally, skipping.", file=sys.stderr)
        return

    mission = load_mission(slug)
    title = mission.get('title', slug)
    print(f"[resync] syncing mission: {slug}")

    # Upsert mission (POST; Taskvia returns existing record if slug already registered)
    _taskvia_request('POST', '/api/missions', {'slug': slug, 'title': title})

    tasks = list_tasks(slug)
    for meta, body in tasks:
        task_id = meta.get('id')
        if not task_id:
            continue
        if meta.get('status') == CORRUPT_TASK_STATUS:
            # A [破損] placeholder is purely a local display artifact
            # (list_tasks synthesizes it so one malformed tNNN.md can't take
            # the rest of the mission down — see CORRUPT_TASK_STATUS). It
            # must never leave the machine: syncing it out would overwrite
            # the real card's title/assignee/blocked_by on Taskvia with the
            # placeholder's blanked-out values, destroying information that
            # is still perfectly recoverable by just fixing the local file
            # (t013 P2 fix). Local-only consumers (_print_mission_summary /
            # _print_mission_detail / dashboard-data) are unaffected.
            print(f"[resync] skipping corrupted task {slug}/{task_id} (local display only, not synced)", file=sys.stderr)
            continue

        # Try to create the task; silently ignored if it already exists
        _taskvia_request('POST', f'/api/missions/{slug}/tasks', {
            'id': task_id,
            'title': meta.get('title', ''),
            'skills': meta.get('skills', []),
            'priority': meta.get('priority', 'medium'),
            'blocked_by': meta.get('blocked_by', []),
        })

        # Always PATCH to sync current status / assignee / result / timestamps
        status = meta.get('status', 'pending')
        patch: dict = {'status': status}
        if meta.get('worker'):
            patch['assignee'] = meta['worker']
        if meta.get('started_at'):
            patch['started_at'] = meta['started_at']
        if status == 'done':
            _, result_text = parse_task_body(body)
            if result_text:
                patch['result'] = result_text
            if meta.get('completed_at'):
                patch['completed_at'] = meta['completed_at']

        _taskvia_request('PATCH', f'/api/missions/{slug}/tasks/{task_id}', patch)

    print(f"[resync] done: {slug} ({len(tasks)} task(s) synced)")


def cmd_resync(args):
    """Resync local mission(s) to Taskvia.

    Usage:
      plan.sh resync <slug>    — resync a specific mission
      plan.sh resync --all     — resync all active missions
    """
    opts, positional = parse_opts(args, {'--all': 'bool'})
    if opts.get('--all'):
        state = load_state()
        slugs = state.get('active_missions') or []
        if not slugs:
            print("[resync] No active missions to resync.", file=sys.stderr)
            return
    elif positional:
        slugs = [positional[0]]
    else:
        die("resync requires <slug> or --all")

    for slug in slugs:
        _resync_one(slug)


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

def _mission_data(slug, archived=False):
    base = os.path.join(ARCHIVE_DIR, slug) if archived else mission_dir(slug)
    mission_path = os.path.join(base, 'mission.yaml')
    text, problem = try_read_queue_file(mission_path)
    if problem is not None:
        # JSON 出力。1 つ読めなくても残りの mission は出す (表示系と同じ向き)。
        mission = {}
    else:
        try:
            mission = parse_yaml(text, source=mission_path)
        except ValueError:
            mission = {}

    tasks_list = list_tasks(slug, base_dir=os.path.join(base, 'tasks'))
    done = sum(1 for (m, _) in tasks_list if m.get('status') == 'done')

    task_data = []
    for meta, body in tasks_list:
        description, _ = parse_task_body(body)
        task_data.append({
            'id': meta.get('id', '?'),
            'title': meta.get('title', '?'),
            'status': meta.get('status', 'pending'),
            'skills': meta.get('skills') or [],
            'worker': meta.get('worker'),
            'priority': meta.get('priority', 'medium'),
            'blocked_by': meta.get('blocked_by') or [],
            'description': description,
        })

    return {
        'slug': slug,
        'title': mission.get('title', '(unnamed)'),
        'status': mission.get('status', 'unknown'),
        'done': done,
        'total': len(tasks_list),
        'archived': archived,
        'tasks': task_data,
    }


def cmd_dashboard_data(args):
    """Output JSON with missions and tasks for the dashboard TUI.

    Usage: plan.sh dashboard-data [--all]
    """
    opts, _ = parse_opts(args, {'--all': 'bool'})
    state = load_state()

    missions = [
        _mission_data(slug)
        for slug in (state.get('active_missions') or [])
        if os.path.exists(mission_dir(slug))
    ]

    archived = []
    if opts.get('--all') and os.path.exists(ARCHIVE_DIR):
        for entry in sorted(os.listdir(ARCHIVE_DIR)):
            full = os.path.join(ARCHIVE_DIR, entry)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, 'mission.yaml')):
                archived.append(_mission_data(entry, archived=True))

    print(json.dumps({'missions': missions, 'archived': archived}, ensure_ascii=False, indent=2))


def _set_aside_stale_handoff(handoff_path):
    """`update --reset` で古い handoff ファイルを同じパスから退避する (削除はしない)。

    handoff は `registry/handoffs/<agent>/<task>_HANDOFF.md` の固定パスに書かれるので、
    差し戻した task を同名 Worker がやり直すと、前回のファイルがそのまま残って
    再提出できる。`<path>.stale-<UTC>` に改名して、情報は残しつつ「今回の handoff」
    とは別物にする。card の handoff_path は Worker が書いた値なので、
    **registry/handoffs の下にあるものしか動かさない** (任意のパスを改名しない)。
    退避に失敗しても reset は止めない (人間が card を見て打つコマンドなので、
    警告して手で片付けてもらう)。
    """
    repo_root = os.environ.get('CREWVIA_REPO_ROOT', REPO_ROOT)
    root = os.path.join(repo_root, 'registry', 'handoffs')
    try:
        real_root = os.path.realpath(root)
        # 相対パスは dispatcher.sh と同じ基準 (registry の親 = repo root) で解く。cwd 基準だと
        # dispatcher が読むのと別のファイルを退避してしまい、古い handoff がそのまま残る。
        # (`plan.sh fail` は相対パスを受け付けないので、ここに来るのは古い card / 手書きの値)
        real = os.path.realpath(os.path.join(repo_root, handoff_path))
        if os.path.commonpath([real_root, real]) != real_root:
            print(f"[plan.sh] handoff は {root} の外にあるため動かしません: {handoff_path}",
                  file=sys.stderr)
            return
        if not os.path.isfile(real):
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = _reserve_unique_path(f"{real}.stale-{stamp}")
        os.replace(real, target)    # 置換されるのは、自分が O_EXCL で確保した空ファイルだけ
        print(f"[plan.sh] 古い handoff を退避しました: {target}")
    except (OSError, ValueError) as e:
        print(f"[plan.sh warn] 古い handoff を退避できませんでした ({handoff_path}): {e}"
              " — 手で片付けてください (同じパスの再提出を防ぐため)", file=sys.stderr)


def _reserve_unique_path(base):
    """`base` (衝突したら `base-1`, `base-2`, ...) を O_EXCL で作り、その名前を返す。

    `os.rename` / `os.replace` は既存の宛先を **黙って置換する**。秒精度のタイムスタンプ
    だけを名前にすると、同じ秒の 2 回目の退避が 1 回目の証拠を消す。「存在しなければ改名」は
    check-then-act で同じ穴が残るので、名前の確保そのものを原子的な O_EXCL にする。
    """
    for n in range(1000):
        candidate = base if n == 0 else f"{base}-{n}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError(f"退避先の名前を確保できませんでした: {base}")


# ---------------------------------------------------------------------------
# cmd_update — safe in-place task frontmatter editor
# ---------------------------------------------------------------------------


def cmd_update(args):
    """Update specific frontmatter fields of an existing task.

    Usage:
      plan.sh update <task_id> [--mission <slug>]
                               [--skills <csv>]
                               [--blocked-by <csv>]
                               [--priority high|medium|low]
                               [--worker <name>]
                               [--status <status>]
                               [--description <text>]
                               [--reset]

    --reset sets: status=pending, worker=null, started_at=null, completed_at=null
    --reset と、status を動かす更新 (failed からの --status 等) は前回の FAIL の証拠
    (handoff_path / fail_head / fail_head_waiver) を card から消し、古い handoff ファイルを
    退避する (t028。knowledge/fail-evidence.md)。
    Body (Description / Result sections) is never modified by this command.

    前提の指定 (--expect-status / --expect-worker / --expect-started-at) は
    ここには無い。デーモンが自動で走らせる後始末は `plan.sh retire` 1 本に
    集約してあり、判定はその内側 — card の書き換えと同じロックの中 — で行う
    (t024)。`update` に同じ前提指定を並べて置くと、「どれを渡すか / 渡さないか」
    の判断が呼び出し側ごとに分かれ、1 つ緩めた場所から同じ型の事故が再発する。
    3 巡のレビューで出た P1 9 件はすべてその形だった。

    `update --reset` は**人間の手作業**用として残してある (Director が幽霊
    task を片付ける経路)。ガードが無いのは弱さではなく、人間が card を見て
    判断したうえで打つコマンドだからである。デーモンからは呼ばないこと。
    """
    opts, positional = parse_opts(args, {
        '--mission': 'value',
        '--skills': 'value',
        '--blocked-by': 'value',
        '--priority': 'value',
        '--worker': 'value',
        '--status': 'value',
        '--description': 'value',
        '--reset': 'bool',
        '--pr-number': 'value',
    })

    if not positional:
        die("update requires a task_id (e.g. t005)")
    task_id = positional[0]

    # Validate task_id format
    if not re.fullmatch(r't\d+', task_id):
        die(f"invalid task_id '{task_id}': expected format tNNN (e.g. t001, t012)")

    # Validate priority if given
    priority = opts.get('--priority')
    if priority and priority not in PRIORITY_ORDER:
        die(f"invalid priority '{priority}'. Use high|medium|low.")

    # Validate status if given
    status = opts.get('--status')
    valid_statuses = {
        'pending', 'in_progress', 'done', 'failed', 'blocked', 'skipped', 'verified',
        'ready_for_verification', 'verifying', 'verification_failed', 'needs_human_review',
    }
    if status and status not in valid_statuses:
        die(f"invalid status '{status}'. Valid statuses: {', '.join(sorted(valid_statuses))}")

    # Holder for reset worker info (populated inside _do, used after with_lock)
    # [0] = old worker name, [1] = slug (for assignment content verification)
    reset_worker_holder = [None, None]
    stale_handoff_holder = [None]  # 証拠を消した card の handoff_path (ファイル退避用)

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            # --mission 省略: 従来は default_mission に黙って当てていた。task id が複数の
            # active mission に在ると、Director / Worker が意図しない mission の同じ tNNN を
            # 書き換える。複数に当たるときは done / fail と同じ規則で決める (曖昧なら拒否)。
            matches = [s for s in (state.get('active_missions') or [])
                       if os.path.exists(task_path(s, task_id))]
            if len(matches) > 1:
                slug = resolve_ambiguous_mission('update', task_id, matches)
            else:
                slug = state.get('default_mission')
        if not slug:
            die("no active mission. Pass --mission <slug> or set a default mission.")
        if not os.path.exists(mission_dir(slug)):
            die(f"mission '{slug}' not found.")

        meta, body = load_task(slug, task_id)
        old_status = meta.get('status')

        changed = []

        if opts.get('--reset'):
            # Capture old worker BEFORE nulling it (needed to clean up assignment file)
            reset_worker_holder[0] = meta.get('worker')
            reset_worker_holder[1] = slug
            meta['status'] = 'pending'
            meta['worker'] = None
            meta['started_at'] = None
            meta['completed_at'] = None
            changed.append('reset(status=pending,worker=null,started_at=null,completed_at=null)')

        if opts.get('--skills') is not None:
            new_skills = [s.strip() for s in opts['--skills'].split(',') if s.strip()]
            meta['skills'] = new_skills
            changed.append(f"skills={new_skills}")

        if opts.get('--blocked-by') is not None:
            raw = opts['--blocked-by'].strip()
            new_blocked = [s.strip() for s in raw.split(',') if s.strip()] if raw else []
            meta['blocked_by'] = new_blocked
            changed.append(f"blocked_by={new_blocked}")
            # 外れた依存の解除が残ると、同じ id を後で付け直したときに
            # 「解除した覚えのない依存」が最初から解除済みになる。
            # 形の違う released_deps (読み取りが隔離する card) は、ここで捨てる。
            # 反復すると `TypeError` / mapping のキーを解除と読む — この直後に
            # blocked_by を組み直す操作なので、捨てても解除が増える方向には働かない。
            old_released = meta.get('released_deps')
            if _TASK_CARDS.released_deps_problem(old_released):
                old_released = []
            kept = [d for d in (old_released or []) if d in new_blocked]
            if kept:
                meta['released_deps'] = kept
            elif 'released_deps' in meta:
                del meta['released_deps']

        if priority:
            meta['priority'] = priority
            changed.append(f"priority={priority}")

        if opts.get('--worker') is not None:
            worker_val = opts['--worker']
            meta['worker'] = None if worker_val.lower() in ('null', 'none', '') else worker_val
            changed.append(f"worker={'null' if meta['worker'] is None else meta['worker']}")

        if status:
            meta['status'] = status
            changed.append(f"status={status}")

        # 前回の FAIL の証拠 (handoff / head) を持ち越さない。古い handoff が同じ固定パスに
        # 残ると、やり直しの FAIL としてそのまま再提出できてしまう (backlog #8 の誘因)。
        # **--reset だけでなく status を動かす経路すべて** (t028): `--status in_progress` /
        # `--status pending` で開き直しても card に証拠が残ると、次の `fail` が handoff を
        # 渡さないとき古い値が failed の card に載り、dispatcher が読んで通知していた。
        # 証拠が生き残るのは「failed のまま failed」(同じ FAIL の再記録) だけ — 逆に
        # 「failed 以外 → failed」は FAIL の報告 (`plan.sh fail`) を経ていないので、card に
        # 載っている証拠は今回の FAIL のものではない。status を触らない更新 (priority 等) は
        # 証拠に関与しない。ファイルの退避は card の保存後に行う (退避であって削除ではない)。
        keeps_evidence = not opts.get('--reset') and (
            not status or (status == 'failed' and old_status == 'failed'))
        if not keeps_evidence:
            stale_handoff_holder[0] = meta.get('handoff_path')
            for stale_key in ('handoff_path', 'fail_head', 'fail_head_waiver'):
                if stale_key in meta:
                    del meta[stale_key]
                    changed.append(f'{stale_key}=cleared')

        if opts.get('--description') is not None:
            # Replace Description section while preserving Result section
            _, result_text = parse_task_body(body)
            body = build_task_body(opts['--description'], result_text)
            changed.append('description=<updated>')

        if opts.get('--pr-number') is not None:
            raw_pr = opts['--pr-number'].strip()
            if raw_pr.lower() in ('null', 'none', ''):
                if 'pr_number' in meta:
                    del meta['pr_number']
                changed.append('pr_number=null')
            else:
                try:
                    n = int(raw_pr)
                except ValueError:
                    die("--pr-number must be a positive integer (or 'null' to clear)")
                if n <= 0:
                    die("--pr-number must be a positive integer (or 'null' to clear)")
                meta['pr_number'] = n
                changed.append(f"pr_number={n}")

        if not changed:
            print(f"update {slug}/{task_id}: nothing to do (no fields specified)", file=sys.stderr)
            return

        save_task(slug, task_id, meta, body)
        print(f"Updated: {slug}/{task_id} — {', '.join(changed)}")

        # Dispatcher Rule 5 の誤報を防ぐため、差し戻した Worker の assignment も
        # 撤去する。以前はこれを with_lock() の *外* で行っていたため、ロックを
        # 離してから削除するまでの間に同名 Worker が pending になった card を
        # pull すると、後任の assignment が「内容が一致する」という理由だけで
        # 消えていた (Codex P1)。card の書き換えと同じトランザクションに入れた
        # ことで、その隙間そのものが無くなっている。
        if stale_handoff_holder[0]:
            _set_aside_stale_handoff(stale_handoff_holder[0])

        old_worker = reset_worker_holder[0]
        reset_slug = reset_worker_holder[1]
        if opts.get('--reset') and old_worker and reset_slug:
            verdict = retire_assignment(old_worker, reset_slug, task_id, None)
            if verdict == ASSIGN_MINE:
                print(f"[plan.sh] Removed stale assignment: {old_worker} → {reset_slug}:{task_id}")
            elif verdict != ASSIGN_ABSENT:
                print(
                    f"[plan.sh warn] {describe_assignment_verdict(old_worker, verdict)}"
                    f" — 削除しませんでした ({old_worker} は別の作業に就いている可能性があります)",
                    file=sys.stderr,
                )

    with_lock(_do)


def cmd_release_dep(args):
    """Director が「failed の依存を持つこの task を進めてよい」と明示的に決める。

    Usage:
      plan.sh release-dep <task_id> [--dep <csv>] [--mission <slug>]

    failed の依存を持つ task は保留 (HELD) になり、pull / dispatch は拒否する
    (t007 / backlog #9)。これはその出口: 対象の依存を card の `released_deps` に
    記録する。`blocked_by` は消さない — DAG に「この task はあの依存に由来する」
    という履歴が残り、release した事実も card から読める。

    --dep を省略すると、**今 failed で解除されていない依存すべて**が対象。--dep で
    名指しした依存は、その task の `blocked_by` に無ければ拒否する (打ち間違いが
    解除に見えてしまうため)。まだ failed でない依存を名指しすると「もし failed に
    なっても待たない」という事前解除になる (今の依存は待ったまま)。

    対象は pending の task だけ。走り出した task の依存を解除しても意味が無く、
    黙って card を書き換えると Worker の実行中に前提が変わる。
    """
    opts, positional = parse_opts(args, {'--mission': 'value', '--dep': 'value'})
    if not positional:
        die("release-dep requires a task_id (e.g. t005)")
    task_id = positional[0]
    if not re.fullmatch(r't\d+', task_id):
        die(f"invalid task_id '{task_id}': expected format tNNN (e.g. t001, t012)")

    def _do():
        state = load_state()
        slug = opts.get('--mission') or state.get('default_mission')
        if not slug:
            die("no active mission. Pass --mission <slug> or set a default mission.")
        if not os.path.exists(mission_dir(slug)):
            die(f"mission '{slug}' not found.")

        meta, body = load_task(slug, task_id)
        st = meta.get('status')
        if st != 'pending':
            die(f"task '{task_id}' is {st}, not pending — release-dep applies only to a "
                f"pending task held by a failed dependency")

        # `load_task()` は生の card を返すので、形の違う `released_deps` (読み取りが
        # `[破損]` に隔離する card) はここで初めて見える。Director が「解除する」と
        # 言っている場面なので、隔離のまま断らず、不正な値は捨てて書き直す —— さもないと
        # 隔離が保留の出口 (この command) を塞ぐ。捨てるのは解除の**記録**で、保留を
        # 外す方向には働かない (捨てた分は、下で名指し / 既定で選び直される)。
        problem = _TASK_CARDS.released_deps_problem(meta.get('released_deps'))
        if problem:
            print(f"[plan.sh warn] {slug}/{task_id}: {problem} — 不正な released_deps は"
                  f"捨てて書き直します", file=sys.stderr)
            meta['released_deps'] = []

        # `load_task()` は生の card なので、形の違う `blocked_by` (読み取りが `[破損]` に
        # 隔離する card) もここでは見える。解除は保留を外す権限なので、何の依存を解除
        # するのか読めない card では断る (`released_deps` と違い、捨てて書き直しはしない:
        # `blocked_by` は依存の**宣言そのもの**で、捨てると制約が消える)。出口は
        # `plan.sh update <id> --blocked-by ...`。
        bb_problem = _TASK_CARDS.blocked_deps_problem(meta.get('blocked_by'))
        if bb_problem:
            die(f"task '{task_id}': {bb_problem}. 先に "
                f"`plan.sh update {task_id} --mission {slug} --blocked-by <ids>` で"
                f"直すこと — release-dep は依存の宣言を読み違えたまま解除しない")
        blocked_by = declared_dependencies(meta.get('blocked_by'))
        tasks = list_tasks(slug, quiet=True)
        done_ids = {m['id'] for (m, _) in tasks if m.get('status') in TERMINAL_STATUSES}
        task_statuses = {m['id']: m.get('status') for (m, _) in tasks}

        if opts.get('--dep') is not None:
            wanted = [d.strip() for d in opts['--dep'].split(',') if d.strip()]
            if not wanted:
                die("--dep is empty. Name the dependency to release (e.g. --dep t003).")
            stray = [d for d in wanted if d not in blocked_by]
            if stray:
                die(f"task '{task_id}' does not depend on {stray} "
                    f"(blocked_by: {blocked_by}) — nothing released")
        else:
            wanted = card_dependencies(meta, done_ids, task_statuses).held
            if not wanted:
                die(f"task '{task_id}' has no held dependency — nothing to release "
                    f"(blocked_by: {blocked_by}). "
                    f"HELD は failed の依存があるときだけ。plan.sh status で確認")

        released = list(meta.get('released_deps') or [])
        added = [d for d in wanted if d not in released]
        meta['released_deps'] = released + added
        save_task(slug, task_id, meta, body)
        after = card_dependencies(meta, done_ids, task_statuses)
        if added:
            print(f"Released: {slug}/{task_id} — {', '.join(added)} "
                  f"(released_deps={meta['released_deps']})")
        else:
            print(f"Released: {slug}/{task_id} — already released ({', '.join(wanted)})")
        if after.unmet:
            print(f"  まだ待つ依存: {after.unmet}")

    with_lock(_do)


# ---------------------------------------------------------------------------
# cmd_retire — 実行アイデンティティで束縛した単一のガード付きトランザクション
# ---------------------------------------------------------------------------

#: retire が終了扱いにできる唯一の状態。
#:
#: 「終わっている状態」を列挙して弾くのではなく、**まだ自分で結末を書いていない
#: 実行**だけを通す形にしてある。列挙は必ず漏れる: needs_director /
#: ready_for_verification / verification_failed はどれも Worker 自身が書いた
#: 結末で、worker も started_at もそのまま残るため、列挙から漏れた瞬間に
#: 「世代まで一致する reset」が成立して結末が消える。
#: これは呼び出し側 (lib_retirement) が持っていた `--expect-status in_progress`
#: を API の内側に取り込んだものでもある (t024)。
RETIRE_RETIRABLE_STATUS = 'in_progress'


def cmd_retire(args):
    """plan.sh retire <task_id> --agent <name> --started-at <generation>
                                [--mission <slug>] [--outcome reset|needs-director]
                                [--reason "<1 行>"] [--no-wait]

    「この実行 (mission, task, worker, 世代) を終了扱いにして後始末する」を
    1 つの操作として提供する。card の status 書き換えと assignment の撤去は
    同じキューロックの中で行われ、どちらも起きるか、どちらも起きないかのどちらか。

    なぜ個別のフィールド指定ではなく 1 本の API なのか
    ----------------------------------------------------
    呼び出し側 (watchdog の retirement 等) は、ロックを取る *前* に「この
    Worker を終了させる」と決める。決定から着弾までの間に card は動きうる:
    Worker が最後に plan.sh done を通していたかもしれないし、人間が差し戻して
    同名の後任が pull し直したかもしれない。呼び出し側が status / worker /
    世代を個別に渡す形だと、どれを渡すか・渡さないかの判断が呼び出し側ごとに
    分かれ、1 つ緩めた場所から同じ型の事故が再発する。判定を 1 箇所に集約し、
    緩める余地を API から無くしてある。

    --started-at が必須なのはそのためである。status と worker は、人間が
    差し戻して同名 Worker が pull し直すと元の値にそのまま戻る (crewvia は
    名前をポジションとして使い回す)。世代 = `pull` が毎回書き換える
    started_at を突き合わせて初めて「この実行」を名指しできる。

    証拠が足りないときの倒し方
    --------------------------
    前提が 1 つでも外れた場合、また assignment の世代を証明できない場合は、
    1 バイトも書かずに exit 3 (PRECONDITION_UNMET) で返る。前提を弱めて
    実行する経路は用意しない — 呼び出し側は保留に倒し、Director に上げること。

    終了させられるのは in_progress の実行だけ
    -----------------------------------------
    status が in_progress 以外の card は、その実行が**自分で結末を書いた**
    ものである (done / failed はもちろん、needs_director /
    ready_for_verification / verification_failed も同じ)。いずれも worker と
    started_at はそのまま残るので、世代まで一致する reset が成立してしまう。
    ここで通してしまうと、Director に上げたはずの card が pending に戻る。
    以前は呼び出し側が `update --expect-status in_progress` として持っていた
    ガードで、t024 でこの API の内側に取り込んだ。
    """
    opts, positional = parse_opts(args, {
        '--mission': 'value',
        '--agent': 'value',
        '--started-at': 'value',
        '--outcome': 'value',
        '--reason': 'value',
        '--no-wait': 'bool',
    })

    if not positional:
        die("retire requires a task_id (e.g. t005)")
    task_id = positional[0]
    if not re.fullmatch(r't\d+', task_id):
        die(f"invalid task_id '{task_id}': expected format tNNN (e.g. t001, t012)")

    agent = (opts.get('--agent') or '').strip()
    if not agent:
        die("retire requires --agent <name> (終了させる実行の Worker 名)")
    require_valid_agent_name(agent)

    # 世代は省略不可。省略を許すと「名前だけで束縛された後始末」に戻ってしまう。
    raw_generation = opts.get('--started-at')
    if raw_generation is None or not raw_generation.strip():
        die(
            "retire requires --started-at <generation>\n"
            "  card の started_at (plan.sh pull が毎回書き換える実行世代) を渡してください。\n"
            "  世代を読めなかった場合は retire を呼ばず、Director に上げること —\n"
            "  名前だけで束縛された後始末は、同名の後任の実行を巻き込みます。"
        )
    generation = raw_generation.strip()
    if generation.lower() in ('null', 'none'):
        die(
            "--started-at null は実行を指していません "
            "(started_at が null の card には終了させるべき実行がありません)"
        )

    outcome = (opts.get('--outcome') or 'reset').strip()
    if outcome not in ('reset', 'needs-director'):
        die(f"invalid --outcome '{outcome}'. Use reset|needs-director.")

    reason = opts.get('--reason')
    if outcome == 'needs-director' and not (reason or '').strip():
        die("--outcome needs-director requires --reason \"<1 行の理由>\"")

    def _do():
        state = load_state()
        slug = opts.get('--mission')
        if not slug:
            matches = [s for s in (state.get('active_missions') or [])
                       if os.path.exists(task_path(s, task_id))]
            if not matches:
                die(f"task '{task_id}' not found in any active mission.")
            if len(matches) > 1:
                die(f"task '{task_id}' exists in multiple missions: {matches}. Use --mission.")
            slug = matches[0]
        if not os.path.exists(task_path(slug, task_id)):
            die(f"task '{task_id}' not found in mission '{slug}'.")

        meta, body = load_task(slug, task_id)

        # ── 前提の確認。ここから下で 1 バイトでも書く前に、全部通す。 ──────
        prefix = f"[plan.sh retire] 前提が外れています ({slug}/{task_id}): "
        suffix = " — 何も変更していません"

        cur_status = meta.get('status')
        if cur_status != RETIRE_RETIRABLE_STATUS:
            die(f"{prefix}status が '{cur_status}' です"
                f" (終了させられるのは '{RETIRE_RETIRABLE_STATUS}' の実行だけ —"
                f" それ以外は実行自身が書いた結末なので、巻き戻しません){suffix}",
                PRECONDITION_UNMET)

        cur_worker = meta.get('worker')
        if cur_worker != agent:
            die(f"{prefix}worker は {cur_worker!r} で、{agent!r} ではありません{suffix}",
                PRECONDITION_UNMET)

        cur_generation = meta.get('started_at')
        if cur_generation is not None:
            cur_generation = str(cur_generation).strip()
        if cur_generation != generation:
            die(f"{prefix}started_at は {cur_generation!r} で、指定された "
                f"{generation!r} と異なります (同じ task の別の実行です){suffix}",
                PRECONDITION_UNMET)

        # assignment は「この実行のもの」と確定したときだけ撤去する。存在しない
        # 場合 (既に片付いた card の取り残し) は撤去すべきものが無いだけなので
        # 続行してよいが、それ以外 — 特に世代を証明できない場合 — は保留に倒す。
        verdict = classify_assignment(agent, slug, task_id, generation)
        if verdict not in (ASSIGN_MINE, ASSIGN_ABSENT):
            die(f"{prefix}{describe_assignment_verdict(agent, verdict)}{suffix}",
                PRECONDITION_UNMET)

        # ── ここから書き込み。前提は全部通っている。 ──────────────────────
        if outcome == 'reset':
            meta['status'] = 'pending'
            meta['worker'] = None
            meta['started_at'] = None
            meta['completed_at'] = None
            applied = 'status=pending, worker=null, started_at=null, completed_at=null'
        else:
            summary, full_text = split_long_freeform(reason)
            meta['status'] = 'needs_director'
            meta['needs_director_reason'] = summary
            if full_text is not None:
                body = body.rstrip() + '\n\n## Needs-Director 詳細\n' + full_text.strip() + '\n'
            applied = f'status=needs_director, needs_director_reason={summary}'

        save_task(slug, task_id, meta, body)
        retire_assignment(agent, slug, task_id, generation)

        print(f"Retired: {slug}/{task_id} — {agent} @ {generation}")
        print(f"[plan.sh] {applied}")
        if verdict == ASSIGN_ABSENT:
            print(f"[plan.sh] assignment/{agent} は既にありませんでした")

    with_lock(_do, nonblocking=bool(opts.get('--no-wait')))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

dispatch = {
    'init': cmd_init,
    'add': cmd_add,
    'pull': cmd_pull,
    'done': cmd_done,
    'needs-director': cmd_needs_director,
    'fail': cmd_fail,
    'update': cmd_update,
    'release-dep': cmd_release_dep,
    'retire': cmd_retire,
    'ready-for-verification': cmd_ready_for_verification,
    'verify-result': cmd_verify_result,
    'review': cmd_review,
    'launch': cmd_launch,
    'task-graph': cmd_task_graph,
    'lint': cmd_lint,
    'status': cmd_status,
    'archive': cmd_archive,
    'resync': cmd_resync,
    'dashboard-data': cmd_dashboard_data,
    'resolve-mission': cmd_resolve_mission,
}

if SUBCOMMAND not in dispatch:
    print(f"Unknown subcommand: {SUBCOMMAND}", file=sys.stderr)
    print(f"Available: {', '.join(dispatch)}", file=sys.stderr)
    sys.exit(1)


def _exit_code_of(exc):
    """SystemExit が運ぶ終了コード。code は int / str / None のいずれでもありうる。"""
    code = exc.code
    if isinstance(code, int):
        return code
    return 0 if code is None else 1


# 生成はここ — キューロックの外、コマンドが終わったあと。
# 失敗した実行のあとでも呼ぶ: 途中まで書いて die() したケースがありうるので、
# 「成功したときだけ」に絞ると DAG がその分だけ古いまま残る。
# 例外は LOCK_BUSY (ロックを取れず 1 バイトも書かずに引き返した実行) で、
# これは watchdog が監視ループの中から同期で叩く経路。何も書いていないと
# 分かっている実行のあとに全 mission を走査し直すのは、キューが混んでいる
# まさにその瞬間に足す純粋な無駄になる。
try:
    dispatch[SUBCOMMAND](ARGS)
except SystemExit as _e:
    # UsageExit (`--help` / 引数の誤り) は何も書いていない: 再生成もしない
    if _exit_code_of(_e) != LOCK_BUSY and not isinstance(_e, UsageExit):
        maybe_refresh_task_graph(SUBCOMMAND)
    raise
else:
    maybe_refresh_task_graph(SUBCOMMAND)
PYEOF
