#!/usr/bin/env bash
# scripts/test_plan_review_cycle_refund.sh
# t011 (mission 20260908-launch-reliability, QA t009 FINDING-B) の回帰テスト。
#
# 背景: plan.sh review (cmd_review) は Step 2 (_do_start) で
# review-plan.sh を呼ぶ*前*に review.cycle_count をインクリメントする。
# review-plan.sh がタイムアウトして「判定不能 (verdict が読めない)」で
# 打ち切った場合 (plan_review.md 自体は書かれている = TIMEOUT_FRESH)、
# 既存コードは mission.status を drafting に戻すだけで cycle_count は
# インクリメントしたまま戻していなかった。コード中のコメントは
# 「cycle_count を無駄に消費させない」意図を謳っていたが、実際には
# 消費されていた (t011 Description 参照)。
#
# 修正: _rollback_to_drafting(reason, refund_cycle=True) で
# review-plan.sh 呼び出し前に先食いした cycle_count を元に戻す。
#
# テスト方法: scripts/plan.sh 本体 (実ファイル) を CREWVIA_QUEUE=<scratch>
# で実行する。scripts/review-plan.sh だけをスタブに差し替える
# (repo_root は python 側で os.path.dirname(QUEUE_DIR) から計算されるため、
# CREWVIA_QUEUE を scratch/queue に向ければ review_script は
# scratch/scripts/review-plan.sh を見る — 実データには一切触れない)。
#
# 検証内容:
#   1. review-plan.sh が「判定不能で打ち切り」(plan_review.md 有り, exit 1)
#      した場合 → mission.status が drafting に戻り、かつ
#      review.cycle_count が呼び出し前の値まで refund されること (消費0)
#   2. review-plan.sh が正常に approve を返した場合 → review.cycle_count は
#      引き続き消費されること (regression: 正常系まで refund してしまわない)
#
# 実行: bash scripts/test_plan_review_cycle_refund.sh
# 副作用: /tmp 配下に一時 queue を作成し終了時に削除する (実データ非破壊)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

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

echo "== test_plan_review_cycle_refund.sh (t011) =="

_cycle_count_of() {
  # mission.yaml の review.cycle_count を雑に grep で取り出す (簡易 YAML なので十分)
  awk '/^  cycle_count:/ { print $2; exit }' "$TMPDIR_TEST/queue/missions/testmission/mission.yaml"
}

_status_of() {
  awk '/^status:/ { print $2; exit }' "$TMPDIR_TEST/queue/missions/testmission/mission.yaml"
}

_setup() {
  TMPDIR_TEST="/tmp/crewvia-test-cycle-refund-$$"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config"
  export CREWVIA_QUEUE="$TMPDIR_TEST/queue"
  unset TASKVIA_URL TASKVIA_TOKEN 2>/dev/null || true

  # cmd_review は _load_lint_module() で os.path.dirname(QUEUE_DIR)/scripts/lint_plan.py
  # を動的 import するため、scratch 側にも実ファイルを置く必要がある
  # (lint のロジック自体はこのテストの対象外なので実物をそのまま使う)。
  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$TMPDIR_TEST/scripts/"
  cp -r "$OWN_CHECKOUT_ROOT/config/." "$TMPDIR_TEST/config/"

  # lint_mission はタスクが1件も無いミッションを無条件で PASS 扱いにする
  # (lint_plan.py: `if not tasks: return 0`) ため、タスクは作らずに済む。
  "$PLAN_SH" init "Test Mission" --mission testmission >/dev/null 2>&1
}

echo ""
echo "--- Case 1 (t011 FINDING-B): review-plan.sh が判定不能で打ち切り (plan_review.md 有り, exit 1) → cycle_count は消費されない ---"
_setup
BEFORE1="$(_cycle_count_of)"
cat > "$TMPDIR_TEST/scripts/review-plan.sh" << 'EOF'
#!/usr/bin/env bash
# スタブ: reviewer が既知の別表記にない語彙 (STOP 等) を書いた場合を模す。
# plan_review.md 自体は書くが verdict は読めない (normalize も失敗する内容)
# ため exit 1 で返す — review-plan.sh の TIMEOUT_FRESH 相当の契約を再現。
SLUG="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MISSION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/queue/missions/$SLUG"
cat > "$MISSION_DIR/plan_review.md" << 'INNER'
## 総合判定

**STOP**
INNER
exit 1
EOF
chmod +x "$TMPDIR_TEST/scripts/review-plan.sh"

set +e
OUT1="$("$PLAN_SH" review testmission 2>&1)"
RC1=$?
set -e
AFTER1="$(_cycle_count_of)"
STATUS1="$(_status_of)"

if [[ "$RC1" -ne 0 && "$AFTER1" == "$BEFORE1" && "$STATUS1" == "drafting" ]]; then
  pass "判定不能で打ち切り → cycle_count 消費なし (before=$BEFORE1 after=$AFTER1), status=drafting に復帰"
else
  fail "判定不能で打ち切り後の期待値と不一致 — rc=$RC1 before=$BEFORE1 after=$AFTER1 status=$STATUS1 output=$OUT1"
fi

echo ""
echo "--- Case 2 (regression): review-plan.sh が approve を返す正常系 → cycle_count は引き続き消費される (refund しない) ---"
_setup
BEFORE2="$(_cycle_count_of)"
cat > "$TMPDIR_TEST/scripts/review-plan.sh" << 'EOF'
#!/usr/bin/env bash
SLUG="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MISSION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/queue/missions/$SLUG"
cat > "$MISSION_DIR/plan_review.md" << 'INNER'
**Verdict:** approve
INNER
exit 0
EOF
chmod +x "$TMPDIR_TEST/scripts/review-plan.sh"

set +e
OUT2="$("$PLAN_SH" review testmission 2>&1)"
RC2=$?
set -e
AFTER2="$(_cycle_count_of)"
STATUS2="$(_status_of)"
EXPECTED2=$((BEFORE2 + 1))

if [[ "$RC2" -eq 0 && "$AFTER2" -eq "$EXPECTED2" && "$STATUS2" == "ready" ]]; then
  pass "approve 正常系 → cycle_count は引き続き消費される (before=$BEFORE2 after=$AFTER2), status=ready"
else
  fail "approve 正常系の期待値と不一致 (regression) — rc=$RC2 before=$BEFORE2 after=$AFTER2 expected=$EXPECTED2 status=$STATUS2 output=$OUT2"
fi

echo ""
echo "--- Case 3 (F2, PR#188 t012 Seo 指摘): review-plan.sh は exit 0 (成功) を返すが plan_review.md の判定語が不正 (approve/revise/reject のいずれでもない) → cycle_count は消費されない ---"
# review-plan.sh 自身が「OK」と判断して exit 0 で返したのに、plan.sh 側の
# 厳密な正規表現 (approve|revise|reject) では読めない、という不整合を直接
# 再現する。以前は _rollback_to_drafting に refund_cycle=True が付いていな
# かったため、reviewer の書式ミスだけで cycle_count が消費されていた。
_setup
BEFORE3="$(_cycle_count_of)"
cat > "$TMPDIR_TEST/scripts/review-plan.sh" << 'EOF'
#!/usr/bin/env bash
SLUG="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MISSION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/queue/missions/$SLUG"
cat > "$MISSION_DIR/plan_review.md" << 'INNER'
**Verdict:** STOP
INNER
exit 0
EOF
chmod +x "$TMPDIR_TEST/scripts/review-plan.sh"

set +e
OUT3="$("$PLAN_SH" review testmission 2>&1)"
RC3=$?
set -e
AFTER3="$(_cycle_count_of)"
STATUS3="$(_status_of)"

if [[ "$RC3" -ne 0 && "$AFTER3" == "$BEFORE3" && "$STATUS3" == "drafting" ]]; then
  pass "不正な判定語 (STOP) → cycle_count 消費なし (before=$BEFORE3 after=$AFTER3), status=drafting に復帰 (F2 fix)"
else
  fail "不正な判定語での期待値と不一致 (F2 regression) — rc=$RC3 before=$BEFORE3 after=$AFTER3 status=$STATUS3 output=$OUT3"
fi

unset CREWVIA_QUEUE

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
