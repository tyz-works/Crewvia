#!/usr/bin/env bash
# scripts/test_review_plan_verdict_binding_e2e.sh
# t015 (mission 20260912-verdict-ci-launcher) の回帰テスト。
#
# 背景: PR #199 head 3388334 (t002 の修正) を Kai-codex (t004) と Finn (t003,
# Opus QA) がそれぞれ独立に検証し、fail-open の欠陥を実測した:
#
#   FAIL-1 / Kai-codex P1 (TOCTOU): scripts/review-plan.sh:300 で
#   PROSE_VERDICT を1回だけ読み、prose=revise/reject の場合は構造化出力を
#   待たずに FINAL=prose で exit 0 する。EXIT trap の mux_kill が走る間に
#   reviewer が plan_review.md の1行目を approve に再 Write すると、
#   scripts/plan.sh (cmd_review) はその**書き換わった後の** plan_review.md を
#   独立に読み直すため、構造化出力の確認なしに approve が消費される
#   (Finn の T1/T1r で decisive に再現)。
#
# 本テストは、実物の `scripts/plan.sh review` → `scripts/review-plan.sh` →
# `scripts/wait_for_plan_review.sh` / `scripts/lib_verdict.py` を通す e2e
# ハーネス (Finn の QA t003 ハーネスと同じ設計) で T1/T1r を再現する。
# 差し替えるのは scratch 側の scripts/lib_mux.sh だけ:
#   - mux_spawn: reviewer の代わりに plan_review.md と PLAN_REVIEWER_LOG を
#     同期的に書く (プローズは最初から読める規定形式にしておく)。
#   - mux_kill: 呼ばれた**その場で** (同期的に) plan_review.md を書き換える
#     — herdr の tab close が SIGHUP 後 2 秒以内に効くまでの間に reviewer が
#     もう1ターン書き込む、という本番の窓を「kill が返る前に書き込みが必ず
#     終わっている」形で決定論的に再現する (Finn の QA t003 と同じ考え方:
#     「kill 開始をシナリオに通知し、書き込み完了を待つ」)。
#   - mux_pid: 書き換えが完了済みなら「見つからない (=停止確認できる)」を
#     返す。
#
# 3388334 (修正前) は T1/T1r で status: ready / last_verdict: approve に
# なることを、本テストで実際に検証している (下記 _verify_regression_on_old
# 参照 — 実行すると一時的に review-plan.sh を旧版に差し替えて red を確認し、
# 元に戻す)。
#
# 実行: bash scripts/test_review_plan_verdict_binding_e2e.sh
#       bash scripts/test_review_plan_verdict_binding_e2e.sh --verify-red
#         (追加で 3388334 相当の review-plan.sh を使い、T1 が red で
#          あることを確認してから元のスクリプトで再実行する)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

# 実行を速くするためだけに、構造化出力待ちの上限を縮める (本番デフォルトは
# 3秒/180秒)。見つかった場合は即座に抜けるので正常系には影響しない。
export REVIEW_PLAN_STRUCTURED_POLL_INTERVAL=1
export REVIEW_PLAN_STRUCTURED_MAX_WAIT=2
export REVIEW_PLAN_STOP_CONFIRM_POLL_INTERVAL=1
export REVIEW_PLAN_STOP_CONFIRM_MAX_SECONDS=2

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_review_plan_verdict_binding_e2e.sh (t015, QA t003 Finn FAIL-1 T1/T1r 実測の恒久化) =="

_cleanup_dir() {
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$1" 2>/dev/null || true
}

# $1 = label, $2 = initial_prose (mux_spawn が同期的に書く plan_review.md の
# 本文), $3 = rewrite_prose ("" なら mux_kill は何もしない。非空なら
# mux_kill が同期的にこの内容へ書き換える), $4 = log_body ("" なら構造化
# 出力は一切出さない), $5 = log_delay_seconds (0 なら mux_spawn が同期的に
# 書く。>0 ならバックグラウンドで指定秒後に書く — F2g 用),
# $6 = expect_status (ready|drafting), $7 = expect_verdict ("" は null 期待),
# $8 = review_plan_sh_path (省略時は実物)
_run_case() {
  local label="$1" initial_prose="$2" rewrite_prose="$3" log_body="$4" \
        log_delay="$5" expect_status="$6" expect_verdict="$7" \
        review_plan_sh_path="${8:-$OWN_CHECKOUT_ROOT/scripts/review-plan.sh}"

  local T="/tmp/crewvia-test-verdict-binding-e2e-$$-$(date +%s%N)"
  _cleanup_dir "$T"
  mkdir -p "$T/scripts" "$T/config"
  export CREWVIA_QUEUE="$T/queue"
  unset TASKVIA_URL TASKVIA_TOKEN 2>/dev/null || true

  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$T/scripts/"
  cp "$OWN_CHECKOUT_ROOT/scripts/lib_verdict.py" "$T/scripts/"
  cp "$OWN_CHECKOUT_ROOT/scripts/lib_model.py" "$T/scripts/"
  cp "$OWN_CHECKOUT_ROOT/scripts/wait_for_plan_review.sh" "$T/scripts/"
  cp "$review_plan_sh_path" "$T/scripts/review-plan.sh"
  cp -r "$OWN_CHECKOUT_ROOT/config/." "$T/config/"
  "$PLAN_SH" init "Test Mission" --mission testmission >/dev/null 2>&1

  local review_output="$T/queue/missions/testmission/plan_review.md"
  local verdict_file="$T/queue/missions/testmission/plan_review.verdict"
  local rewrite_flag="$T/rewrite_done.flag"

  # スタブ lib_mux.sh。mux_spawn/mux_kill は sourced function として
  # review-plan.sh 自身のプロセスで実行されるため、中で使う $$ は
  # review-plan.sh の PID と一致する (PLAN_REVIEWER_LOG の実際のパスと合わせる
  # ため)。
  cat > "$T/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  printf '%s\n' '${initial_prose}' > "${review_output}"
  if [[ "${log_delay}" -gt 0 ]]; then
    ( sleep ${log_delay}; printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\$\$.log" ) &
    disown 2>/dev/null || true
  else
    printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\$\$.log"
  fi
  return 0
}
mux_kill() {
  # Finn の QA t003 と同じ考え方: kill が返る前に、herdr の SIGHUP 猶予
  # (最大2秒) の間に起きうる reviewer のもう1回の Write を**同期的に**
  # 完了させてから返す。これにより「kill 呼び出し後・停止確認前」の窓を
  # 決定論的に踏む。
  if [[ -n "${rewrite_prose}" ]]; then
    printf '%s\n' '${rewrite_prose}' > "${review_output}"
  fi
  touch "${rewrite_flag}"
  return 0
}
mux_pid() {
  # 書き換え (または no-op の場合はそもそも rewrite_flag) が完了していれば
  # 「見つからない」(exit 1) = 停止確認できる。
  if [[ -f "${rewrite_flag}" ]]; then
    return 1
  fi
  echo 999999
  return 0
}
EOF

  set +e
  OUT="$("$PLAN_SH" review testmission 2>&1)"
  set -e

  local status verdict got_verdict_file="<none>"
  status="$(awk '/^status:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  verdict="$(awk '/^  last_verdict:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  [[ -f "$verdict_file" ]] && got_verdict_file="$(cat "$verdict_file")"

  local status_ok="false" verdict_ok="false"
  [[ "$status" == "$expect_status" ]] && status_ok="true"
  if [[ -z "$expect_verdict" ]]; then
    [[ "$verdict" == "null" || -z "$verdict" ]] && verdict_ok="true"
  else
    [[ "$verdict" == "$expect_verdict" ]] && verdict_ok="true"
  fi

  if [[ "$status_ok" == "true" && "$verdict_ok" == "true" ]]; then
    pass "$label — status=$status verdict=$verdict (plan_review.verdict=$got_verdict_file)"
  else
    fail "$label — expected status=$expect_status verdict=${expect_verdict:-null}, got status=$status verdict=$verdict (plan_review.verdict=$got_verdict_file). plan.sh review output: $OUT"
  fi
  _cleanup_dir "$T"
}

RESULT_JSON_APPROVE='{"type":"result","subtype":"success","is_error":false,"structured_output":{"verdict":"approve"}}'

echo ""
echo "--- T1 (FAIL-1, Kai-codex P1): prose=revise で検証 → kill 中に reviewer が approve へ再 Write, structured 無し ---"
echo "    修正前 (3388334) は status: ready / verdict: approve になっていた (下の --verify-red 参照)。"
_run_case "T1 (revise → kill 中に approve へ再Write, structured無し)" \
  '**Verdict:** revise' '**Verdict:** approve' '' 0 \
  "drafting" ""

echo ""
echo "--- T1r (FAIL-1 亜種): prose=reject で検証 → kill 中に approve へ再 Write ---"
_run_case "T1r (reject → kill 中に approve へ再Write, structured無し)" \
  '**Verdict:** reject' '**Verdict:** approve' '' 0 \
  "drafting" ""

echo ""
echo "--- 対照 (regression なし): prose=revise で検証 → 書き換えなし、structured 無し → 正しく revise として消費 ---"
_run_case "対照: revise, 書き換えなし → drafting/revise" \
  '**Verdict:** revise' '' '' 0 \
  "drafting" "revise"

echo ""
echo "--- 対照 (regression なし): prose=approve, structured=approve 即時 → ready/approve (正常系が壊れていないこと) ---"
_run_case "対照: approve + structured=approve (即時) → ready/approve" \
  '**Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 \
  "ready" "approve"

echo ""
echo "--- F2c (Director 設計判断2/3 の正常系): prose=approve, structured=approve が遅延到着 → ready/approve (手動確認に落ちない) ---"
_run_case "F2c: approve + structured=approve (1秒遅延) → ready/approve" \
  '**Verdict:** approve' '' "$RESULT_JSON_APPROVE" 1 \
  "ready" "approve"

echo ""
echo "--- F2g (Finn 実測, Director 設計判断2): prose=revise, structured=approve が遅延到着 → 食い違いとして fail-closed (以前は revise を待たず消費し、この食い違いを検出できなかった) ---"
RESULT_JSON_APPROVE_FOR_F2G='{"type":"result","subtype":"success","is_error":false,"structured_output":{"verdict":"approve"}}'
_run_case "F2g: revise(prose) + structured=approve (1秒遅延) → 食い違い検出、drafting/null (cycle refund)" \
  '**Verdict:** revise' '' "$RESULT_JSON_APPROVE_FOR_F2G" 1 \
  "drafting" ""

echo ""
echo "--- Decision1 直接確認: plan_review.md が review-plan.sh 終了後に外部から書き換わっても、plan.sh は plan_review.verdict の値だけを使う ---"
# review-plan.sh 自体は正常終了 (revise, 書き換えなし) させ、plan.sh が
# plan_review.md を読む**前**に plan_review.md を外部から approve に
# 書き換える — もし plan.sh が (t015 以前のように) plan_review.md を
# 独立に読み直していたら、これは approve になってしまう。
T_BIND="/tmp/crewvia-test-verdict-binding-direct-$$"
_cleanup_dir "$T_BIND"
mkdir -p "$T_BIND/scripts" "$T_BIND/config"
export CREWVIA_QUEUE="$T_BIND/queue"
cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$T_BIND/scripts/"
cp "$OWN_CHECKOUT_ROOT/scripts/lib_verdict.py" "$T_BIND/scripts/"
cp -r "$OWN_CHECKOUT_ROOT/config/." "$T_BIND/config/"
"$PLAN_SH" init "Test Mission" --mission testmission >/dev/null 2>&1
BIND_REVIEW_OUTPUT="$T_BIND/queue/missions/testmission/plan_review.md"
BIND_VERDICT_FILE="$T_BIND/queue/missions/testmission/plan_review.verdict"
cat > "$T_BIND/scripts/review-plan.sh" << EOF
#!/usr/bin/env bash
set -uo pipefail
SLUG="\$1"
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
MISSION_DIR="\$(cd "\$SCRIPT_DIR/.." && pwd)/queue/missions/\$SLUG"
REVIEW_OUTPUT="\$MISSION_DIR/plan_review.md"
VERDICT_FILE="\$MISSION_DIR/plan_review.verdict"
rm -f "\$REVIEW_OUTPUT" "\$VERDICT_FILE"
printf '%s\n' '**Verdict:** revise' > "\$REVIEW_OUTPUT"
printf '%s\n' 'revise' > "\${VERDICT_FILE}.tmp" && mv "\${VERDICT_FILE}.tmp" "\$VERDICT_FILE"
# review-plan.sh が確定して exit した**後**に、何者か (herdr の遅延書き込み・
# 別プロセス等) が plan_review.md を書き換える、という最悪ケースを模す。
printf '%s\n' '**Verdict:** approve' > "\$REVIEW_OUTPUT"
exit 0
EOF
chmod +x "$T_BIND/scripts/review-plan.sh"
set +e
BIND_OUT="$("$PLAN_SH" review testmission 2>&1)"
set -e
BIND_STATUS="$(awk '/^status:/ { print $2; exit }' "$T_BIND/queue/missions/testmission/mission.yaml" 2>/dev/null)"
BIND_VERDICT="$(awk '/^  last_verdict:/ { print $2; exit }' "$T_BIND/queue/missions/testmission/mission.yaml" 2>/dev/null)"
if [[ "$BIND_STATUS" == "drafting" && "$BIND_VERDICT" == "revise" ]]; then
  pass "Decision1: plan_review.md の事後書き換え (revise→approve) は無視され、plan_review.verdict の revise が消費される — status=$BIND_STATUS verdict=$BIND_VERDICT"
else
  fail "Decision1: plan_review.md の事後書き換えが消費されてしまった (TOCTOU regression) — status=$BIND_STATUS verdict=$BIND_VERDICT (output: $BIND_OUT)"
fi
_cleanup_dir "$T_BIND"

unset CREWVIA_QUEUE

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="

if [[ "${1:-}" == "--verify-red" ]]; then
  echo ""
  echo "== --verify-red: 3388334 相当の review-plan.sh + 旧 plan.sh (plan_review.md 独立読み直し) で T1 が red になることを確認 =="
  # 旧 plan.sh は自分自身の場所から REPO_ROOT (= dirname(plan.sh)/..) を
  # 解決し、そこから scripts/lib_verdict.py 等を動的 import するため、
  # 単なる裸ファイルではなく scripts/ ディレクトリ構造の中に置く必要がある。
  OLD_REPO="/tmp/crewvia-test-verdict-binding-old-repo-$$"
  _cleanup_dir "$OLD_REPO"
  mkdir -p "$OLD_REPO/scripts"
  git -C "$OWN_CHECKOUT_ROOT" show HEAD:scripts/review-plan.sh > "$OLD_REPO/scripts/review-plan.sh" 2>/dev/null
  git -C "$OWN_CHECKOUT_ROOT" show HEAD:scripts/plan.sh > "$OLD_REPO/scripts/plan.sh" 2>/dev/null
  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$OLD_REPO/scripts/"
  git -C "$OWN_CHECKOUT_ROOT" show HEAD:scripts/lib_verdict.py > "$OLD_REPO/scripts/lib_verdict.py" 2>/dev/null
  chmod +x "$OLD_REPO/scripts/plan.sh" "$OLD_REPO/scripts/review-plan.sh"
  ORIG_PLAN_SH="$PLAN_SH"
  PLAN_SH="$OLD_REPO/scripts/plan.sh"
  PASS_COUNT=0
  FAIL_COUNT=0
  _run_case "[RED CHECK] T1 on 3388334 (旧 review-plan.sh + 旧 plan.sh)" \
    '**Verdict:** revise' '**Verdict:** approve' '' 0 \
    "drafting" "" "$OLD_REPO/scripts/review-plan.sh"
  if [[ "$FAIL_COUNT" -ge 1 ]]; then
    echo "  (confirmed) 旧コードでは T1 が期待どおり FAIL する = fail-open regression を再現できている"
  else
    echo "  (info) 旧コードでも T1 が PASS した — 旧コードの再現条件を見直す必要がある"
  fi
  _cleanup_dir "$OLD_REPO"
  PLAN_SH="$ORIG_PLAN_SH"
fi

[[ "$FAIL_COUNT" -eq 0 ]]
