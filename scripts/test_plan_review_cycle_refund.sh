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
#
# t015 (mission 20260912-verdict-ci-launcher, Director 設計判断1) 追記:
# plan.sh はもう plan_review.md を独立に読み直さない — review-plan.sh が
# 書く queue/missions/<slug>/plan_review.verdict だけを消費する (QA t003
# Finn 実測 FAIL-1 の TOCTOU 対策)。
#
# t018 (同 mission, QA t016 Finn E_A3 / Director 追記の低優先度項目) 追記:
# plan.sh は plan_review.verdict が「今回の review-plan.sh 実行で書かれた」
# ことを自分で確認する。実行ごとの識別子を CREWVIA_PLAN_REVIEW_RUN_ID で
# 渡し、ファイルは "<verdict>\nrun_id=<識別子>\n" の 2 行ちょうどで
# なければ消費しない (呼び出し前に古いファイルも消す)。Case 2/3 のスタブは
# この形式で書く。Case 4-6 は鮮度確認そのものの回帰テスト。

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

_last_verdict_of() {
  awk '/^  last_verdict:/ { print $2; exit }' "$TMPDIR_TEST/queue/missions/testmission/mission.yaml"
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
printf '%s\nrun_id=%s\n' 'approve' "$CREWVIA_PLAN_REVIEW_RUN_ID" > "$MISSION_DIR/plan_review.verdict"
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
echo "--- Case 3 (F2, PR#188 t012 Seo 指摘 / t015 で plan_review.verdict 契約に更新): review-plan.sh は exit 0 (成功) を返すが plan_review.verdict の判定語が不正 (approve/revise/reject のいずれでもない) → cycle_count は消費されない ---"
# review-plan.sh 自身が「OK」と判断して exit 0 で返したのに、plan.sh 側の
# allowlist (approve|revise|reject) では読めない、という不整合を直接
# 再現する。以前は _rollback_to_drafting に refund_cycle=True が付いていな
# かったため、reviewer の書式ミスだけで cycle_count が消費されていた。
# t015: 正しく実装された review-plan.sh はもう plan_review.verdict に不正
# な値を書いて exit 0 することは無いはずだが、plan.sh 側の防御的
# allowlist チェック (ファイル破損等への備え) がまだ効いていることを
# 直接確認する。run_id は正しく書く (鮮度確認ではなく値の確認を通すため)。
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
printf '%s\nrun_id=%s\n' 'STOP' "$CREWVIA_PLAN_REVIEW_RUN_ID" > "$MISSION_DIR/plan_review.verdict"
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

# $1 = ラベル, $2 = 事前に置く plan_review.verdict の内容 ("" なら置かない),
# $3 = スタブ review-plan.sh が書く plan_review.verdict の内容 (printf の
# フォーマット文字列。"__NONE__" なら書かない。%s は CREWVIA_PLAN_REVIEW_RUN_ID)
_run_freshness_case() {
  local label="$1" preexisting="$2" stub_format="$3"
  _setup
  local before after status verdict out rc
  before="$(_cycle_count_of)"
  if [[ -n "$preexisting" ]]; then
    printf '%s' "$preexisting" > "$TMPDIR_TEST/queue/missions/testmission/plan_review.verdict"
  fi
  cat > "$TMPDIR_TEST/scripts/review-plan.sh" << EOF
#!/usr/bin/env bash
SLUG="\$1"
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
MISSION_DIR="\$(cd "\$SCRIPT_DIR/.." && pwd)/queue/missions/\$SLUG"
printf '%s\n' '**Verdict:** approve' > "\$MISSION_DIR/plan_review.md"
if [[ '$stub_format' != "__NONE__" ]]; then
  printf '$stub_format' "\${CREWVIA_PLAN_REVIEW_RUN_ID:-}" > "\$MISSION_DIR/plan_review.verdict"
fi
exit 0
EOF
  chmod +x "$TMPDIR_TEST/scripts/review-plan.sh"

  set +e
  out="$("$PLAN_SH" review testmission 2>&1)"
  rc=$?
  set -e
  after="$(_cycle_count_of)"
  status="$(_status_of)"
  verdict="$(_last_verdict_of)"
  if [[ "$rc" -ne 0 && "$after" == "$before" && "$status" == "drafting" && ( "$verdict" == "null" || -z "$verdict" ) ]]; then
    pass "$label → 消費されず refund (rc=$rc before=$before after=$after status=$status last_verdict=${verdict:-null})"
  else
    fail "$label → refund を期待したが rc=$rc before=$before after=$after status=$status last_verdict=$verdict output=$out"
  fi
}

echo ""
echo "--- Case 4 (t018, Finn E_A3): 前 cycle の approve が残っていて、review-plan.sh が rm も書き込みもせず exit 0 → 古い approve を消費しない ---"
_run_freshness_case "E_A3: 古い plan_review.verdict (approve) + 何も書かない review-plan.sh" \
  $'approve\nrun_id=0123456789abcdef0123456789abcdef\n' "__NONE__"

echo ""
echo "--- Case 5 (t018): review-plan.sh が値は正しいが別の実行の識別子で書いた → 消費しない ---"
_run_freshness_case "run_id 不一致 (approve / run_id=not-this-run)" \
  "" 'approve\nrun_id=not-this-run\n'

echo ""
echo "--- Case 6 (t018): 識別子の無い旧形式 (t015 の 1 行ファイル) → 消費しない ---"
_run_freshness_case "旧形式 1 行 (approve\\n)" \
  "" 'approve\n'

echo ""
echo "--- Case 7 (t018): 識別子の後に余計な行がある → 消費しない (2 行ちょうどでなければ fail-closed) ---"
_run_freshness_case "余計な 3 行目 (approve / run_id=<正> / approve)" \
  "" 'approve\nrun_id=%s\napprove\n'

unset CREWVIA_QUEUE

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
