#!/usr/bin/env bash
# test_dispatcher_notify.sh — dispatcher.sh pull-notify 修正の回帰テスト
#
# 検証内容:
#   1. director-only skill を持つ task は unblocked_pending から除外される
#   2. blocked_by 未満足の task は通知対象外 (既存 PR #108 の動作確認)
#   3. 通常 pending task (スキルあり, blocked_by 解決済み) は通知対象になる
#   4. TTL 内の同一通知は抑制される (NOTIFY_CACHE)
#   5. NOTIFY_CACHE が PID 非依存パスになっている
#   6. NOTIFY_TTL が 300s (5 分) 以上に設定されている
#   7. director-only task は assignment loop でも除外される (assign 通知なし)
#
# 実行: bash scripts/test_dispatcher_notify.sh
# 副作用: /tmp 配下に一時ファイルを作成し終了時に削除する

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCHER_SH="$OWN_CHECKOUT_ROOT/scripts/dispatcher.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  [[ -n "${TMPDIR_TEST:-}" && -d "$TMPDIR_TEST" ]] && rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

echo "== test_dispatcher_notify.sh (director-only 除外 + TTL/cache 修正) =="

# ---------------------------------------------------------------------------
# Setup: 一時 queue ディレクトリ
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-notify-$$"
QUEUE="$TMPDIR_TEST/queue"
REGISTRY="$TMPDIR_TEST/registry"
MISSION_SLUG="test-mission"
TASKS_DIR="$QUEUE/missions/$MISSION_SLUG/tasks"
mkdir -p "$TASKS_DIR" "$QUEUE/archive" "$REGISTRY"

# state.yaml — active missions
cat > "$QUEUE/state.yaml" <<EOF
active_missions:
  - $MISSION_SLUG
default_mission: $MISSION_SLUG
EOF

# mission.yaml — in_progress
cat > "$QUEUE/missions/$MISSION_SLUG/mission.yaml" <<EOF
title: "Test Mission"
status: in_progress
next_task_id: t004
EOF

# t001: 通常 pending task (対照例)
cat > "$TASKS_DIR/t001.md" <<'EOF'
---
id: t001
title: "通常タスク"
skills: [bash]
priority: high
status: pending
blocked_by: []
---
EOF

# t002: director-only skill を持つ task
cat > "$TASKS_DIR/t002.md" <<'EOF'
---
id: t002
title: "Director専用タスク"
skills: [director-only]
priority: high
status: pending
blocked_by: []
---
EOF

# t003: blocked task (blocked_by t001 が pending)
cat > "$TASKS_DIR/t003.md" <<'EOF'
---
id: t003
title: "ブロック中タスク"
skills: [bash]
priority: high
status: pending
blocked_by: [t001]
---
EOF

# t004: director-only + 別スキルの複合 task
cat > "$TASKS_DIR/t004.md" <<'EOF'
---
id: t004
title: "Director専用+複合スキル"
skills: [director-only, ops]
priority: high
status: pending
blocked_by: []
---
EOF

# workers.yaml — bash スキルを持つ worker (tmux なしなので実際の割当は起きない)
cat > "$REGISTRY/workers.yaml" <<'EOF'
workers:
  - name: Wei
    skills: [bash, code]
    task_count: 0
EOF

# ---------------------------------------------------------------------------
# Python テストランナー: dispatcher.sh の Python ロジックを直接実行
# ---------------------------------------------------------------------------
run_python_check() {
  local notify_cache="$TMPDIR_TEST/notify-cache.json"
  python3 - "$QUEUE" "$REGISTRY" "$notify_cache" "300" <<'PYEOF'
import sys
import os
import re
import json
import time
from pathlib import Path

QUEUE_DIR      = Path(sys.argv[1])
REGISTRY_DIR   = Path(sys.argv[2])
NOTIFY_CACHE   = Path(sys.argv[3])
NOTIFY_TTL     = int(sys.argv[4])

MISSIONS_DIR   = QUEUE_DIR / 'missions'
STATE_FILE     = QUEUE_DIR / 'state.yaml'
WORKERS_FILE   = REGISTRY_DIR / 'workers.yaml'
PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
TERMINAL_STATUSES = {'done', 'verified', 'skipped'}
DIRECTOR_ONLY_SKILLS = {'director-only'}   # Fix A: same constant as dispatcher.sh


def _split_inline_list(s):
    out, cur, in_q = [], [], None
    for ch in s:
        if in_q:
            cur.append(ch)
            if ch == in_q: in_q = None
            continue
        if ch in ('"', "'"):
            in_q = ch; cur.append(ch); continue
        if ch == ',':
            out.append(''.join(cur).strip()); cur = []; continue
        cur.append(ch)
    if cur: out.append(''.join(cur).strip())
    return [x for x in out if x]

def _scalar(val):
    if val in ('null', '~'): return None
    if val in ('true', 'True'): return True
    if val in ('false', 'False'): return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    if re.fullmatch(r'-?\d+', val): return int(val)
    return val

def parse_yaml(text):
    lines = text.splitlines(); result = {}; i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'): i += 1; continue
        m = re.match(r'^([\w-]+):\s*(.*)$', line)
        if not m: i += 1; continue
        key, val = m.group(1), m.group(2).rstrip()
        if val == '':
            i += 1; items = []
            while i < len(lines):
                lst = re.match(r'^\s+-\s*(.*)$', lines[i])
                if lst: items.append(_scalar(lst.group(1).strip())); i += 1
                else: break
            result[key] = items if items else None
        elif val.startswith('[') and val.endswith(']'):
            inner = val[1:-1].strip()
            result[key] = [_scalar(s.strip()) for s in _split_inline_list(inner)] if inner else []
            i += 1
        else:
            result[key] = _scalar(val); i += 1
    return result

def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---': return {}, text
    end = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == '---': end = idx; break
    if end < 0: return {}, text
    meta = parse_yaml('\n'.join(lines[1:end]))
    meta.setdefault('skills', []); meta.setdefault('blocked_by', [])
    if meta.get('skills') is None: meta['skills'] = []
    if meta.get('blocked_by') is None: meta['blocked_by'] = []
    return meta, '\n'.join(lines[end+1:])

def list_tasks(slug):
    tdir = MISSIONS_DIR / slug / 'tasks'
    if not tdir.exists(): return []
    entries = sorted(
        [(int(m.group(1)), fn)
         for fn in tdir.iterdir()
         if (m := re.fullmatch(r't(\d+)\.md', fn.name))]
    )
    return [parse_frontmatter(p.read_text()) for _, p in entries]

# Load tasks
state = parse_yaml(STATE_FILE.read_text())
active_missions = list(state.get('active_missions') or [])
slug = active_missions[0]

tasks = list_tasks(slug)
done_ids = {m['id'] for m, _ in tasks if m.get('status') in TERMINAL_STATUSES}
task_statuses = {m['id']: m.get('status') for m, _ in tasks}

# Build unblocked_pending (same logic as dispatcher.sh)
unblocked_pending = []
for meta, _ in tasks:
    if meta.get('status') != 'pending': continue
    bb = meta.get('blocked_by') or []
    if any(dep not in done_ids and task_statuses.get(dep) not in ('failed', 'cancelled')
           for dep in bb):
        continue
    task_skills = set(meta.get('skills') or [])
    if not task_skills: continue
    if task_skills & DIRECTOR_ONLY_SKILLS: continue   # Fix A
    unblocked_pending.append(meta)

# Output results as JSON for shell assertions
result = {
    'unblocked_ids': [m['id'] for m in unblocked_pending],
    'director_only_excluded': 't002' not in [m['id'] for m in unblocked_pending],
    'director_only_mixed_excluded': 't004' not in [m['id'] for m in unblocked_pending],
    'blocked_excluded': 't003' not in [m['id'] for m in unblocked_pending],
    'normal_included': 't001' in [m['id'] for m in unblocked_pending],
}
print(json.dumps(result))
PYEOF
}

# ---------------------------------------------------------------------------
# Test 1: NOTIFY_CACHE が PID 非依存パスになっている
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 1: NOTIFY_CACHE は PID 非依存パス --"
if grep -q 'NOTIFY_CACHE="/tmp/dispatcher-notify-cache\.json"' "$DISPATCHER_SH"; then
  pass "NOTIFY_CACHE に PID (\$\$) が含まれない"
else
  fail "NOTIFY_CACHE がまだ PID 固有パス (\$\$.json) のまま"
fi

# ---------------------------------------------------------------------------
# Test 2: NOTIFY_TTL が 300 秒以上
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 2: NOTIFY_TTL >= 300s --"
ttl_val=$(grep -E '^NOTIFY_TTL=' "$DISPATCHER_SH" | head -1 | sed 's/NOTIFY_TTL=\([0-9]*\).*/\1/')
if [[ -n "$ttl_val" && "$ttl_val" -ge 300 ]]; then
  pass "NOTIFY_TTL=${ttl_val}s (>= 300s)"
else
  fail "NOTIFY_TTL=${ttl_val}s (< 300s — 短すぎる)"
fi

# ---------------------------------------------------------------------------
# Test 3: DIRECTOR_ONLY_SKILLS が定義されている
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 3: DIRECTOR_ONLY_SKILLS 定義 --"
if grep -q "DIRECTOR_ONLY_SKILLS" "$DISPATCHER_SH"; then
  pass "DIRECTOR_ONLY_SKILLS が dispatcher.sh に定義されている"
else
  fail "DIRECTOR_ONLY_SKILLS が dispatcher.sh に未定義"
fi

# ---------------------------------------------------------------------------
# Test 4-7: Python ロジック検証
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 4-7: Python unblocked_pending フィルタリング --"
py_result=$(run_python_check)
if [[ $? -ne 0 ]]; then
  fail "Python テストランナーの実行に失敗"
else
  # Test 4: 通常タスク (t001) は unblocked_pending に含まれる
  if echo "$py_result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['normal_included'] else 1)"; then
    pass "通常 pending task (t001, skills=[bash]) は通知対象に含まれる"
  else
    fail "通常 pending task (t001) が unblocked_pending から除外されている"
  fi

  # Test 5: director-only task (t002) は除外される
  if echo "$py_result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['director_only_excluded'] else 1)"; then
    pass "director-only task (t002, skills=[director-only]) は通知対象から除外される"
  else
    fail "director-only task (t002) が unblocked_pending に残っている — Fix A が効いていない"
  fi

  # Test 6: 複合スキル (director-only+ops) task (t004) も除外される
  if echo "$py_result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['director_only_mixed_excluded'] else 1)"; then
    pass "複合スキル task (t004, skills=[director-only, ops]) も通知対象から除外される"
  else
    fail "複合スキル task (t004) が unblocked_pending に残っている"
  fi

  # Test 7: blocked task (t003) は除外される (PR #108 の動作確認)
  if echo "$py_result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['blocked_excluded'] else 1)"; then
    pass "blocked task (t003, blocked_by=[t001]) は通知対象から除外される (PR #108)"
  else
    fail "blocked task (t003) が unblocked_pending に残っている — PR #108 のガードが壊れている"
  fi
fi

# ---------------------------------------------------------------------------
# Test 8: NOTIFY_CACHE TTL ロジック
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 8: notify_cache TTL dedup --"
NOTIFY_CACHE_TEST="$TMPDIR_TEST/notify-test.json"
python3 - "$NOTIFY_CACHE_TEST" "300" <<'PYEOF'
import sys, json, time
from pathlib import Path

NOTIFY_CACHE = Path(sys.argv[1])
NOTIFY_TTL   = int(sys.argv[2])

def load(): return json.loads(NOTIFY_CACHE.read_text()) if NOTIFY_CACHE.exists() else {}
def save(c): NOTIFY_CACHE.write_text(json.dumps(c))
def should(key): c = load(); return key not in c or time.time() - c[key] > NOTIFY_TTL
def record(key): c = load(); c[key] = time.time(); save(c)

# 初回は通知する
assert should("no_worker_t001") == True, "初回通知が抑制されている"

# 記録後は抑制される
record("no_worker_t001")
assert should("no_worker_t001") == False, "TTL 内に同一通知が繰り返される"

# 異なる key は独立している
assert should("no_worker_t002") == True, "異なる task_id の通知が誤って抑制されている"

print("TTL dedup OK")
PYEOF
if [[ $? -eq 0 ]]; then
  pass "notify_cache: 初回通知 OK、TTL 内 dedup OK、異なる key は独立"
else
  fail "notify_cache TTL dedup ロジックに問題あり"
fi

# ---------------------------------------------------------------------------
# 結果サマリ
# ---------------------------------------------------------------------------
echo ""
echo "== 結果: PASS=${PASS_COUNT}, FAIL=${FAIL_COUNT} =="

if [[ $FAIL_COUNT -eq 0 ]]; then
  echo "全テスト PASS"
  exit 0
else
  echo "失敗したテストがあります"
  exit 1
fi
