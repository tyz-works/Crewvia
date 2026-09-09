#!/usr/bin/env bash
# scripts/test_plan_review_verdict_e2e_variants.sh
# t010 (mission 20260909-dead-config-sweep, QA t008 FINDING-1/2/3) の
# エンドツーエンド回帰テスト。
#
# 背景: QA (t008) は「reviewer が approve を一度も選んでいないのに mission
# が status: ready / last_verdict: approve になる経路」を、実 scripts/plan.sh
# review をエンドツーエンドで走らせて3つ実測した (FINDING-1/2/3。詳細は
# queue/missions/20260909-dead-config-sweep/tasks/t008.md の Result 参照)。
# scripts/lib_verdict.py の単体テスト (test_lib_verdict.sh) だけでは
# 「plan.sh review が実際にどう振る舞うか」(lint → reviewing → review-plan.sh
# → verdict 抽出 → rollback/cycle refund → mission 更新) までは検証できない
# ため、本ファイルは QA t008 の検証方法をそのまま恒久テスト化する。
#
# テスト方法: scripts/test_plan_review_cycle_refund.sh と同じ手法 — 実物の
# scripts/plan.sh を CREWVIA_QUEUE=<scratch> で実行し、scratch 側の
# scripts/review-plan.sh だけを差し替える。差し替え版は claude を一切
# 起動せず、plan_review.md を指定内容で書いた後、**本物の**
# scripts/wait_for_plan_review.sh (内部で本物の lib_verdict.py /
# normalize_plan_review_verdict.py を呼ぶ) を実行し、その OK/TIMEOUT 判定を
# review-plan.sh の終了コード規約 (OK なら0、それ以外1) として再現する。
# plan.sh 側の verdict 抽出・rollback・cycle refund・mission 更新はすべて
# 本物のコードパスを通る。claude CLI は一切起動しない。
#
# 実行: bash scripts/test_plan_review_verdict_e2e_variants.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

_cleanup_dir() {
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$1" 2>/dev/null || true
}

echo "== test_plan_review_verdict_e2e_variants.sh (t010, QA t008 FINDING-1/2/3) =="

# $1 = variant名, $2 = plan_review.md に書く本文 ("__NO_FILE__" ならファイル
# を一切作らない = missing_file 相当), $3 = 期待する status
# (ready|drafting), $4 = 期待する last_verdict ("" なら null 期待), $5 = label
_run_variant() {
  local name="$1" body="$2" expect_status="$3" expect_verdict="$4" label="$5"
  local T="/tmp/crewvia-test-verdict-e2e-$$-${name}"
  _cleanup_dir "$T"
  mkdir -p "$T/scripts" "$T/config"
  export CREWVIA_QUEUE="$T/queue"
  unset TASKVIA_URL TASKVIA_TOKEN 2>/dev/null || true
  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$T/scripts/"
  cp -r "$OWN_CHECKOUT_ROOT/config/." "$T/config/"
  "$PLAN_SH" init "Test Mission" --mission testmission >/dev/null 2>&1

  local write_file="true"
  [[ "$body" == "__NO_FILE__" ]] && write_file="false"

  cat > "$T/scripts/review-plan.sh" << EOF
#!/usr/bin/env bash
set -uo pipefail
SLUG="\$1"
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
MISSION_DIR="\$(cd "\$SCRIPT_DIR/.." && pwd)/queue/missions/\$SLUG"
REVIEW_OUTPUT="\$MISSION_DIR/plan_review.md"
rm -f "\$REVIEW_OUTPUT"
START=\$(date +%s)
if [[ "$write_file" == "true" ]]; then
cat > "\$REVIEW_OUTPUT" << 'INNER'
$body
INNER
fi
WAIT_OUTPUT="\$(bash "$OWN_CHECKOUT_ROOT/scripts/wait_for_plan_review.sh" "\$REVIEW_OUTPUT" "\$START" 2 1)"
WAIT_RC=\$?
WAIT_STATUS="\$(printf '%s\n' "\$WAIT_OUTPUT" | head -1)"
[[ "\$WAIT_RC" -eq 0 && "\$WAIT_STATUS" == "OK" ]] && exit 0
exit 1
EOF
  chmod +x "$T/scripts/review-plan.sh"

  set +e
  OUT="$("$PLAN_SH" review testmission 2>&1)"
  set -e
  local status verdict
  status="$(awk '/^status:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  verdict="$(awk '/^  last_verdict:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"

  local status_ok="false" verdict_ok="false"
  [[ "$status" == "$expect_status" ]] && status_ok="true"
  if [[ -z "$expect_verdict" ]]; then
    [[ "$verdict" == "null" || -z "$verdict" ]] && verdict_ok="true"
  else
    [[ "$verdict" == "$expect_verdict" ]] && verdict_ok="true"
  fi

  if [[ "$status_ok" == "true" && "$verdict_ok" == "true" ]]; then
    pass "$label — status=$status verdict=$verdict"
  else
    fail "$label — expected status=$expect_status verdict=${expect_verdict:-null}, got status=$status verdict=$verdict (output: $OUT)"
  fi
  _cleanup_dir "$T"
}

echo ""
echo "--- 正常系 (regression: approve/revise/reject/GO表記 は従来どおり動く) ---"
_run_variant "canon_approve" '**Verdict:** approve' "ready" "approve" \
  "規定形式 approve → status: ready, verdict: approve"
_run_variant "canon_revise" '**Verdict:** revise' "drafting" "revise" \
  "規定形式 revise → status: drafting, verdict: revise"
_run_variant "canon_reject" '**Verdict:** reject' "drafting" "reject" \
  "規定形式 reject → status: drafting, verdict: reject"
_run_variant "go_normalized" '## 総合判定: **GO**' "ready" "approve" \
  "別表記 (## 総合判定: **GO**) → normalize されて approve のまま (regression なし)"

echo ""
echo "--- FINDING-1: コードフェンス内にだけ判定がある → 誤 approve にならない ---"
_run_variant "fence_only" '見本:

```
**Verdict:** approve
```

実際の判定はまだ書いていません。' "drafting" "" \
  "フェンス内だけの approve → status: drafting (誤 approve だった旧挙動の修正確認)"

echo ""
echo "--- FINDING-2 [最重要]: フェンス内 approve が後続の本物の revise を上書きしない ---"
_run_variant "mixed_fence" '書式例:

```
**Verdict:** approve
```

実際の判定は以下です。

**Verdict:** revise' "drafting" "revise" \
  "フェンス内approve + 本物のrevise → revise が採用される (旧挙動は誤って approve になっていた)"

echo ""
echo "--- FINDING-3: テンプレート行をそのまま貼ると誤 approve にならない ---"
_run_variant "template_line" '**Verdict:** approve | revise | reject

# Plan Review: test' "drafting" "" \
  "未編集のテンプレート行 (approve | revise | reject) → status: drafting (旧挙動は誤って approve だった)"

echo ""
echo "--- 判定不能の一般化 (regression なし) ---"
_run_variant "no_verdict" '# body only, no verdict language' "drafting" "" \
  "判定語なし → status: drafting"
_run_variant "empty_file" '' "drafting" "" \
  "空ファイル → status: drafting"
_run_variant "missing_file" "__NO_FILE__" "drafting" "" \
  "plan_review.md が作られない → status: drafting"
_run_variant "unknown_word" '**Verdict:** STOP' "drafting" "" \
  "未知の判定語 (STOP) → status: drafting"
_run_variant "negated_go" '## 総合判定: GO は出せない' "drafting" "" \
  "否定形 (GO は出せない) → status: drafting"

unset CREWVIA_QUEUE

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
