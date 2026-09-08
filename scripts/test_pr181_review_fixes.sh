#!/usr/bin/env bash
# test_pr181_review_fixes.sh — t013 回帰テスト
#
# Seo (Opus 5) による PR#181 (t009) レビュー指摘 2 件 (P2) への対応を検証する。
# 指摘全文は PR#181 のコメント参照。
#
# ---------------------------------------------------------------------------
# [P2-1] '## Needs-Director 詳細' の全文が次の done/fail で消える
# ---------------------------------------------------------------------------
# cmd_done / cmd_fail は `desc, _ = parse_task_body(body)` で Description だけを
# 取り出し、`build_task_body(desc, result)` で Description/Result の2セクション
# だけを再構築するため、cmd_needs_director が追記した独立セクション
# ('## Needs-Director 詳細') は次の done/fail で丸ごと破棄されていた。
# これは稀ケースではなく「needs_director → update --reset → 再実行 → done」という
# 通常フローそのもの。
#
# 修正 (採用: 案(a) trailing section を保存して付け直す):
#   extract_trailing_body_section() で Description/Result 以外の trailing
#   section を抽出し、cmd_done/cmd_fail が body を再構築した後に
#   append_trailing_body_section() で付け直す。
#
# 採用理由: 案(b) (独立セクションを作らず Result 内に追記する) は、
# cmd_done/cmd_fail が「CLI から渡された新しい result で Result を完全に
# 置き換える」という既存の意味論 (Result = その回の最終報告) を変えない限り
# 意味がない (旧 Result の内容は新しい result 引数で丸ごと上書きされるため、
# Result の中に埋め込むだけでは同じ問題が起きる)。Result の意味論を
# 「置き換え」から「積み上げ」に変えるのは QA gate 検証・dashboard 表示・
# Taskvia sync 等 Result を「単一の最終報告」として扱う既存コードへの影響が
# 大きく、本 task のスコープを超える。trailing section を独立に保存・復元
# する案(a)の方が既存の意味論を変えずに済み、影響範囲が小さい。
#
# なお update --description (:2713-2714 相当) は元々 `_, result_text =
# parse_task_body(body)` で result 側 (trailing section 込み) をそのまま
# 素通しするため保持されていた。今回の修正で done/fail 経路も同じ「保持され
# る」側に揃え、経路による非対称を解消した。
#
# ---------------------------------------------------------------------------
# [P2-2] corrupted 疑似タスクが Taskvia resync で本物のカードを上書きする
# ---------------------------------------------------------------------------
# _resync_one は list_tasks(slug) の結果をそのまま Taskvia に POST/PATCH する。
# t009 で導入した [破損] 疑似タスク (CORRUPT_TASK_STATUS) は表示専用の
# ローカルプレースホルダだが、_resync_one がそのまま外部に同期すると、
# 本物のカードの title/assignee/blocked_by がプレースホルダの値で上書きされる。
#
# 修正: _resync_one のループ先頭で CORRUPT_TASK_STATUS を skip する。
# 表示専用の consumer (_print_mission_summary / _print_mission_detail /
# dashboard-data) はローカル JSON 出力のみで外部同期しないため対象外のまま。
#
# 実行: bash scripts/test_pr181_review_fixes.sh
# 副作用: /tmp 配下に一時 queue + ローカル限定 (127.0.0.1) の使い捨て HTTP モック
#         サーバーを作成し、終了時にすべて削除・kill する
#         (crewvia の実データ・実 Taskvia には一切触れない)。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
MOCK_SERVER_PID=""
cleanup() {
  if [[ -n "$MOCK_SERVER_PID" ]]; then
    kill "$MOCK_SERVER_PID" 2>/dev/null || true
    wait "$MOCK_SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_pr181_review_fixes.sh (t013: PR#181 review P2 x2) =="

# ---------------------------------------------------------------------------
# Setup: 一時 queue (実データには一切触れない)
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-t013-$$"
QUEUE="$TMPDIR_TEST/queue"
MISSION_SLUG="test-t013-mission"
TASKS_DIR="$QUEUE/missions/$MISSION_SLUG/tasks"
mkdir -p "$TASKS_DIR" "$QUEUE/archive"

printf 'active_missions:\n  - %s\ndefault_mission: %s\n' \
  "$MISSION_SLUG" "$MISSION_SLUG" > "$QUEUE/state.yaml"
printf 'title: Test Mission t013\nslug: %s\nstatus: in_progress\ncreated_at: 2026-09-08T00:00:00Z\ncompleted_at: null\nnext_task_id: 9\n' \
  "$MISSION_SLUG" > "$QUEUE/missions/$MISSION_SLUG/mission.yaml"

# t001: in_progress — needs-director → reset → done の完全フロー用
printf -- '---\nid: t001\ntitle: Task for needs-director-then-done flow\nskills: [bash]\npriority: medium\nstatus: in_progress\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-08T00:00:00Z\ncompleted_at: null\n---\n\n## Description\nDo the thing.\n\n## Result\n' \
  > "$TASKS_DIR/t001.md"

# t002: in_progress — needs-director → reset → fail の完全フロー用
printf -- '---\nid: t002\ntitle: Task for needs-director-then-fail flow\nskills: [bash]\npriority: medium\nstatus: in_progress\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-08T00:00:00Z\ncompleted_at: null\n---\n\n## Description\nDo another thing.\n\n## Result\n' \
  > "$TASKS_DIR/t002.md"

# t003: 意図的に壊れた frontmatter → corrupted pseudo-task (resync 除外対象)
printf -- '---\nid: t003\ntitle: Corrupted task\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: null\nneeds_director_reason: "broken\n=== leaked ===\nplaceholder"\n---\n\n## Description\nCorrupted.\n\n## Result\n' \
  > "$TASKS_DIR/t003.md"

# t004: 健全な pending task — resync が正常に動くことの対照
printf -- '---\nid: t004\ntitle: Healthy task for resync sanity check\nskills: [bash]\npriority: medium\nstatus: pending\nblocked_by: []\nworker: null\nstarted_at: null\ncompleted_at: null\n---\n\n## Description\nHealthy.\n\n## Result\n' \
  > "$TASKS_DIR/t004.md"

run_plan_rc() {
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" > /dev/null 2>&1
}
run_plan_all() {
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" "$@" 2>&1 || true
}

# LONG_REASON: split_long_freeform の要約が FREEFORM_SUMMARY_LIMIT (200) を
# 超えるようにする — 短い複数行入力は t013 P3 fix により frontmatter の要約
# だけで完結し body には退避されない (test_plan_reason_frontmatter.sh の方で
# 別途カバー済み)。ここで検証したいのは「実際に body へ退避された全文が、
# その後の done/fail を経ても保持されること」なので、確実に退避される長さに
# しておく。
LONG_REASON="=== QA Report ===
Environment: WSL2, 6GB memory
Steps: ran the full regression suite, observed a crash in the frontmatter
parser after roughly 40 iterations, captured a stack trace and attached logs
Conclusion: needs Director review before proceeding, do not resume automatically"

# ---------------------------------------------------------------------------
# [P2-1] Test 1-3: needs-director → update --reset → done で全文が保持される
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 1: needs-director (long reason) on t001 appends '## Needs-Director 詳細' ---"
run_plan_rc needs-director t001 "$LONG_REASON" --mission "$MISSION_SLUG"
if grep -q '## Needs-Director 詳細' "$TASKS_DIR/t001.md" && grep -q 'Conclusion: needs Director review' "$TASKS_DIR/t001.md"; then
  pass "t001.md has the Needs-Director 詳細 appendix with the full text"
else
  fail "t001.md should have the Needs-Director 詳細 appendix with the full text"
fi

echo ""
echo "--- Test 2: update --reset + in_progress + done → appendix survives (the normal flow) ---"
run_plan_rc update t001 --reset --mission "$MISSION_SLUG"
run_plan_rc update t001 --status in_progress --worker sofia --mission "$MISSION_SLUG"
run_plan_rc done t001 "All good now, QA passed." --mission "$MISSION_SLUG"
if grep -q '## Needs-Director 詳細' "$TASKS_DIR/t001.md" \
   && grep -q 'Conclusion: needs Director review' "$TASKS_DIR/t001.md" \
   && grep -q 'All good now, QA passed.' "$TASKS_DIR/t001.md"; then
  pass "appendix AND the new done result both survive in t001.md after the full flow"
else
  fail "appendix should survive plan.sh done (P2-1 regression) — content: $(cat "$TASKS_DIR/t001.md")"
fi

echo ""
echo "--- Test 3: same flow but ending in 'fail' instead of 'done' ---"
run_plan_rc needs-director t002 "$LONG_REASON" --mission "$MISSION_SLUG"
run_plan_rc update t002 --reset --mission "$MISSION_SLUG"
run_plan_rc update t002 --status in_progress --worker sofia --mission "$MISSION_SLUG"
run_plan_rc fail t002 "registry/handoffs/sofia/t002_HANDOFF.md" --mission "$MISSION_SLUG"
if grep -q '## Needs-Director 詳細' "$TASKS_DIR/t002.md" \
   && grep -q 'Conclusion: needs Director review' "$TASKS_DIR/t002.md" \
   && grep -q 'FAILED' "$TASKS_DIR/t002.md"; then
  pass "appendix survives plan.sh fail too"
else
  fail "appendix should survive plan.sh fail (P2-1 regression) — content: $(cat "$TASKS_DIR/t002.md")"
fi

echo ""
echo "--- Test 4: plan.sh status still works after the full flow (no corruption introduced) ---"
status_out=$(run_plan_all status --mission "$MISSION_SLUG")
if echo "$status_out" | grep -q 't001' && ! echo "$status_out" | grep -q '破損.*t001'; then
  pass "status stays healthy after needs-director → reset → done/fail flow"
else
  fail "status should stay healthy — got: $status_out"
fi

# ---------------------------------------------------------------------------
# [P2-2] Test 5-6: corrupted task excluded from Taskvia resync
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 5: corrupted task (t003) is excluded from 'plan.sh resync' ---"

# 127.0.0.1 限定の使い捨てモック Taskvia サーバー。受信した全リクエストの
# method/path/body を1行1JSONでログに追記し、常に 200 {} を返す。
MOCK_PORT="$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")"
MOCK_LOG="$TMPDIR_TEST/mock_requests.log"
: > "$MOCK_LOG"
MOCK_SERVER_PY="$TMPDIR_TEST/mock_taskvia.py"
cat > "$MOCK_SERVER_PY" << 'PYEOF'
import http.server, json, sys

LOG_PATH, PORT = sys.argv[1], int(sys.argv[2])

class Handler(http.server.BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            payload = json.loads(raw) if raw else None
        except Exception:
            payload = raw.decode('utf-8', errors='replace')
        with open(LOG_PATH, 'a') as f:
            f.write(json.dumps({'method': self.command, 'path': self.path, 'body': payload}) + '\n')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def log_message(self, *args):
        pass  # silence default access log to stderr

http.server.HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
PYEOF

python3 "$MOCK_SERVER_PY" "$MOCK_LOG" "$MOCK_PORT" &
MOCK_SERVER_PID=$!

# サーバーが listen を開始するまで待つ (最大 ~2秒、ポーリング)
for _ in $(seq 1 20); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$MOCK_PORT") 2>/dev/null; then
    exec 3>&- 3<&-
    break
  fi
  sleep 0.1
done

TASKVIA_URL="http://127.0.0.1:${MOCK_PORT}" TASKVIA_TOKEN="dummy-test-token" \
  CREWVIA_QUEUE="$QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
  bash "$PLAN_SH" resync "$MISSION_SLUG" > /dev/null 2>/tmp/test_t013_resync_stderr

kill "$MOCK_SERVER_PID" 2>/dev/null || true
wait "$MOCK_SERVER_PID" 2>/dev/null || true
MOCK_SERVER_PID=""

if grep -q '"id": "t003"' "$MOCK_LOG" 2>/dev/null || grep -q 'Corrupted task' "$MOCK_LOG" 2>/dev/null; then
  fail "corrupted task t003 must never appear in an outbound Taskvia request — leaked: $(cat "$MOCK_LOG")"
else
  pass "corrupted task t003 never appears in any outbound Taskvia request"
fi

echo ""
echo "--- Test 6: healthy task (t004) IS still synced (sanity check resync isn't just silently broken) ---"
if grep -q '"id": "t004"' "$MOCK_LOG" 2>/dev/null; then
  pass "healthy task t004 is still synced to Taskvia as normal"
else
  fail "healthy task t004 should still be synced — mock log: $(cat "$MOCK_LOG" 2>/dev/null || echo '(empty)')"
fi

echo ""
echo "--- Test 7: plan.sh resync logs that it skipped the corrupted task (visibility, not silent) ---"
if grep -q 'skipping corrupted task' /tmp/test_t013_resync_stderr 2>/dev/null; then
  pass "resync logs a visible skip message for the corrupted task"
else
  fail "resync should log a visible skip message — stderr: $(cat /tmp/test_t013_resync_stderr 2>/dev/null)"
fi

echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
