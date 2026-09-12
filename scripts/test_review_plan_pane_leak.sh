#!/usr/bin/env bash
# scripts/test_review_plan_pane_leak.sh
# t011 (mission 20260908-launch-reliability, QA t009 FINDING-B) の回帰テスト。
#
# 背景: scripts/review-plan.sh は成功時 (WAIT_STATUS == "OK") にしか
# mux_kill を呼んでいなかった。タイムアウト (TIMEOUT_FRESH / TIMEOUT_NONE)
# や異常終了の経路では Plan Reviewer の mux window が残り続け、QA (t009) が
# 手動で kill する事態になった。修正: タイムアウト経路でも mux_kill する。
#
# テスト方法: scripts/review-plan.sh 本体 (実ファイルをそのままコピー) を
# scratch ディレクトリで実行し、以下の2つを実ファイルではなくスタブに
# 差し替える:
#   - lib_mux.sh   : mux_available/mux_spawn を no-op 成功として、
#                    mux_kill が呼ばれたことをログファイルに記録する
#   - wait_for_plan_review.sh : 実際にポーリングせず即座に
#                    OK / TIMEOUT_FRESH / TIMEOUT_NONE を返す
#     (ポーリング判定自体の正しさは scripts/test_wait_for_plan_review.sh が
#     別途検証している。ここでは review-plan.sh 側の「結果に応じて
#     mux_kill するか」だけを claude CLI を一切起動せず高速に検証する)
#
# 検証内容:
#   1. OK (成功) → mux_kill が呼ばれる (既存の正常系、regression 確認)
#   2. TIMEOUT_FRESH → mux_kill が呼ばれる (本修正の本体)
#   3. TIMEOUT_NONE  → mux_kill が呼ばれる (本修正の本体)
#
# 実行: bash scripts/test_review_plan_pane_leak.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REVIEW_PLAN="${SCRIPT_DIR}/review-plan.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_review_plan_pane_leak.sh (t011) =="

_run_case() {
  local wait_status="$1" expect_rc="$2" label="$3"

  TMPDIR_TEST="/tmp/crewvia-test-review-plan-pane-leak-$$-${wait_status}"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission"

  cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
  # t001 (mission 20260909-dead-config-sweep): review-plan.sh はモデル解決に
  # scripts/lib_model.py と config/crewvia.yaml を必須で読むようになった。
  cp "${SCRIPT_DIR}/lib_model.py" "$TMPDIR_TEST/scripts/"
  cp "$(dirname "$SCRIPT_DIR")/config/crewvia.yaml" "$TMPDIR_TEST/config/"

  local kill_log="$TMPDIR_TEST/mux_kill.log"
  : > "$kill_log"

  # スタブ lib_mux.sh: mux_available/mux_spawn は常に成功 (no-op)、
  # mux_kill は呼び出しをログに記録するだけ。
  cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() { return 0; }
mux_kill() { echo "killed:\$1" >> "$kill_log"; return 0; }
EOF

  # スタブ wait_for_plan_review.sh: 実際のポーリングをせず、指定した
  # WAIT_STATUS を即座に返す (ポーリング判定自体は別テストで検証済み)。
  local wait_rc=0
  [[ "$wait_status" != "OK" ]] && wait_rc=1
  cat > "$TMPDIR_TEST/scripts/wait_for_plan_review.sh" << EOF
#!/usr/bin/env bash
echo "$wait_status"
exit $wait_rc
EOF
  chmod +x "$TMPDIR_TEST/scripts/wait_for_plan_review.sh"

  set +e
  bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_stdout 2>&1
  local rc=$?
  set -e

  if [[ "$rc" -ne "$expect_rc" ]]; then
    fail "$label — expected review-plan.sh exit $expect_rc, got $rc (log: $(cat /tmp/test_review_plan_stdout))"
    return
  fi
  if grep -q '^killed:' "$kill_log"; then
    pass "$label — mux_kill called (no pane leak)"
  else
    fail "$label — mux_kill was NOT called (pane leak) — review-plan.sh exit=$rc"
  fi
}

echo ""
echo "--- Case 1: WAIT_STATUS=OK → mux_kill called (regression) ---"
_run_case "OK" 0 "OK (success)"

echo ""
echo "--- Case 2 (t011 FINDING-B): WAIT_STATUS=TIMEOUT_FRESH → mux_kill called ---"
_run_case "TIMEOUT_FRESH" 1 "TIMEOUT_FRESH"

echo ""
echo "--- Case 3 (t011 FINDING-B): WAIT_STATUS=TIMEOUT_NONE → mux_kill called ---"
_run_case "TIMEOUT_NONE" 1 "TIMEOUT_NONE"

echo ""
echo "--- Case 4 (F4, PR#188 t012 Seo 指摘): SIGTERM で中断された場合も mux_kill が呼ばれる (以前は成功時/タイムアウト時の明示 kill だけで、割り込み経路は未カバーだった) ---"
TMPDIR_TEST="/tmp/crewvia-test-review-plan-pane-leak-$$-SIGTERM"
rm -rf "$TMPDIR_TEST"
mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission"
cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
# t001 (mission 20260909-dead-config-sweep): review-plan.sh はモデル解決に
# scripts/lib_model.py と config/crewvia.yaml を必須で読むようになった。
cp "${SCRIPT_DIR}/lib_model.py" "$TMPDIR_TEST/scripts/"
cp "$(dirname "$SCRIPT_DIR")/config/crewvia.yaml" "$TMPDIR_TEST/config/"
KILL_LOG="$TMPDIR_TEST/mux_kill.log"
: > "$KILL_LOG"
cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() { return 0; }
mux_kill() { echo "killed:\$1" >> "$KILL_LOG"; return 0; }
EOF
# wait_for_plan_review.sh の代わりに、spawn 後〜末尾の間 (待機中) を模した
# 長時間 sleep のスタブを置く — この間に外側から SIGTERM を送る。
cat > "$TMPDIR_TEST/scripts/wait_for_plan_review.sh" << 'EOF'
#!/usr/bin/env bash
sleep 30
echo "OK"
exit 0
EOF
chmod +x "$TMPDIR_TEST/scripts/wait_for_plan_review.sh"

bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_stdout 2>&1 &
CHILD_PID=$!
# wait_for_plan_review.sh の sleep に入っているタイミングを見計らって SIGTERM
sleep 1
kill -TERM "$CHILD_PID" 2>/dev/null || true
wait "$CHILD_PID" 2>/dev/null
if grep -q '^killed:' "$KILL_LOG"; then
  pass "SIGTERM 中断 → mux_kill called via trap (F4 fix, no pane leak)"
else
  fail "SIGTERM 中断で mux_kill が呼ばれていない (F4 regression, pane leak) — output=$(cat /tmp/test_review_plan_stdout)"
fi
rm -rf "$TMPDIR_TEST"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
