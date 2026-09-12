#!/usr/bin/env bash
# scripts/test_review_plan_json_rescue.sh
# t004 (mission 20260909-dead-config-sweep) の回帰テスト。
#
# 背景: agents/plan_reviewer.md は plan_review.md の1行目に規定形式
# `**Verdict:** approve|revise|reject` を書くようプロンプトで指示していたが、
# plan-reviewer (Opus) がその指示を外し ## 総合判定: **GO** のような別表記で
# 出力する事故が繰り返し発生していた (scripts/normalize_plan_review_verdict.py
# のヒューリスティックで拾えない場合、600s タイムアウト → Director の手動介入)。
#
# 修正: scripts/review-plan.sh は plan-reviewer 起動時に `claude --json-schema`
# (config/plan-review-verdict.schema.json) で最終応答を強制する。プローズ
# (plan_review.md の規定形式1行目) が読めなかった場合はこの構造化出力から
# verdict を機械的に回収し、plan_review.md の冒頭に規定形式の行を書き込む
# rescue 経路として動く。
#
# F2 追記 (t002, mission 20260912-verdict-ci-launcher): 以前はプローズが
# 既に読めていた (=WAIT_STATUS=="OK") 場合、構造化出力の確認を一度も
# 待たずに (1回だけ非同期に読んで) プローズをそのまま採用していた。
# 構造化出力は必ずプローズより後に届くため、prose=approve の経路では
# この「食い違い検出」が実質 armed されず、reviewer が最終応答で revise/
# reject と言っていても mission が ready になってしまっていた。
# 修正後は、プローズが approve (=危険な結論) または判定不能の場合は
# 構造化出力の到着を (固定間隔ポーリング、最大 REVIEW_PLAN_STRUCTURED_MAX_WAIT
# 秒) 待ち、確認できなければ approve を通さない。revise/reject はプローズを
# 直接信頼してよい (安全な結論のため待たない)。
#
# テスト方法: scripts/test_review_plan_pane_leak.sh と同じ手法 —
# scripts/review-plan.sh 本体を scratch ディレクトリにコピーし、
# lib_mux.sh (mux_spawn を no-op 成功にしつつ、$$ — review-plan.sh 自身の
# PID、source されたシェル関数なので $$ は呼び出し元と同じ — を使って
# PLAN_REVIEWER_LOG に claude --output-format json 相当の出力を模したログを
# 書く) と wait_for_plan_review.sh (プローズ解析の結果を模して即座に返す)
# をスタブに差し替える。claude CLI は一切起動しない。
#
# 検証内容:
#   1. プローズ判定不能 + ログに有効な structured_output.verdict
#      → rescue が発火し、plan_review.md 冒頭に規定形式の行が書かれ、
#      review-plan.sh は exit 0 (Director の手動介入を回避できる本体)
#   2. 同上 (plan_review.md が一度も書かれなかった場合でも rescue で
#      新規作成できる)
#   3. プローズ判定不能 + ログが空/JSON として壊れている
#      → rescue は発火せず、既存どおり exit 1 (当て推量で verdict を
#      捏造しない、既存のフェイルクローズ挙動に回帰がないことの確認)
#   4. プローズ=revise (真の判定行あり) + 構造化出力が一切無い
#      → 待たずにプローズをそのまま採用し exit 0 (安全な結論は待たせない)
#   5〜8. t012 由来 (食い違い検出・rescue・fail-closed の既存回帰、変更なし)
#   9 (F2 コア回帰): プローズ=approve + 構造化出力が最後まで確認できない
#      → 確認なしに approve を通さない (安全側)。これが本 PR の直接の動機。
#      7642f9b (修正前) では WAIT_STATUS=="OK" 経路がこの確認を一度もせず
#      exit 0 のまま approve を通していた。
#   10 (F2 コア回帰・遅延到着): プローズ=approve が即座に読める一方、
#      構造化出力 (=revise, 食い違い) がわずかに遅れて書かれる
#      → 7642f9b は「プローズが既に読めていれば1回だけ非同期に読んで
#      終わり」だったため、この遅延書き込みを永遠に見逃し exit 0 のまま
#      approve を通していた。修正後は固定間隔ポーリングが遅延書き込みに
#      追いつき、食い違いを検出して exit 1 になることを確認する。
#
# 実行: bash scripts/test_review_plan_json_rescue.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REVIEW_PLAN="${SCRIPT_DIR}/review-plan.sh"
REAL_SCHEMA="${SCRIPT_DIR}/../config/plan-review-verdict.schema.json"
# t001 (mission 20260909-dead-config-sweep): review-plan.sh はモデル解決に
# scripts/lib_model.py と config/crewvia.yaml を必須で読むようになった
# (scripts/test_review_plan_director_identity.sh と同じ理由)。
REAL_LIB_MODEL="${SCRIPT_DIR}/lib_model.py"
REAL_CREWVIA_YAML="${SCRIPT_DIR}/../config/crewvia.yaml"

# review-plan.sh の構造化出力待ちループの poll 間隔・上限を、テスト実行を
# 速くするためだけに縮める (本番デフォルトは 3 秒 / 180 秒)。「待っても
# 見つからず諦める」ケース (Case 3, 8, 9) が本番の上限のまま待つと大幅に
# 遅くなるため。見つかった場合は即座に抜けるので、成功系のケースには
# 影響しない。
export REVIEW_PLAN_STRUCTURED_POLL_INTERVAL=1
export REVIEW_PLAN_STRUCTURED_MAX_WAIT=2

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_review_plan_json_rescue.sh (t004) =="

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

# $1 = wait_status, $2 = ログ内容 ("" なら書かない), $3 = expect_rc,
# $4 = expect_verdict_line_present (true/false), $5 = label
_run_case() {
  local wait_status="$1" log_body="$2" expect_rc="$3" expect_line="$4" label="$5"

  TMPDIR_TEST="/tmp/crewvia-test-review-plan-json-rescue-$$-${wait_status}-$(date +%s%N)"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission"

  cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
  cp "$REAL_SCHEMA" "$TMPDIR_TEST/config/plan-review-verdict.schema.json"
  cp "$REAL_LIB_MODEL" "$TMPDIR_TEST/scripts/lib_model.py"
  cp "$REAL_CREWVIA_YAML" "$TMPDIR_TEST/config/crewvia.yaml"

  # スタブ lib_mux.sh: mux_available/mux_spawn は常に成功 (no-op)。mux_spawn は
  # sourced function として review-plan.sh 自身のプロセスで実行されるため、
  # 中で使う $$ は review-plan.sh の PID と一致する — これを使って本物の
  # PLAN_REVIEWER_LOG (/tmp/plan_reviewer_$$.log) にログを書き込める。
  cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  if [[ -n "${log_body}" ]]; then
    printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\$\$.log"
  else
    : > "/tmp/plan_reviewer_\$\$.log"
  fi
  return 0
}
mux_kill() { return 0; }
EOF

  local wait_rc=0
  [[ "$wait_status" != "OK" ]] && wait_rc=1
  cat > "$TMPDIR_TEST/scripts/wait_for_plan_review.sh" << EOF
#!/usr/bin/env bash
echo "$wait_status"
exit $wait_rc
EOF
  chmod +x "$TMPDIR_TEST/scripts/wait_for_plan_review.sh"

  set +e
  bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_json_rescue_stdout 2>&1
  local rc=$?
  set -e

  if [[ "$rc" -ne "$expect_rc" ]]; then
    fail "$label — expected review-plan.sh exit $expect_rc, got $rc (log: $(cat /tmp/test_review_plan_json_rescue_stdout))"
    return
  fi

  local review_output="$TMPDIR_TEST/queue/missions/testmission/plan_review.md"
  local has_line="false"
  if [[ -f "$review_output" ]] && grep -Eq '^\*\*Verdict:\*\*[[:space:]]*(approve|revise|reject)\b' "$review_output"; then
    has_line="true"
  fi

  if [[ "$has_line" == "$expect_line" ]]; then
    pass "$label — exit=$rc, canonical verdict line present=$has_line (as expected)"
  else
    fail "$label — exit=$rc but canonical verdict line present=$has_line (expected $expect_line). plan_review.md: $(cat "$review_output" 2>/dev/null || echo '<missing>')"
  fi
}

RESULT_JSON_REVISE='{"type":"result","subtype":"success","is_error":false,"result":"{\"verdict\":\"revise\"}","structured_output":{"verdict":"revise"}}'

echo ""
echo "--- Case 1: プローズ判定不能 (plan_review.md 未作成) + 有効な structured_output.verdict → rescue が発火し OK 扱い ---"
_run_case "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" 0 "true" "prose unreadable + valid structured output"

echo ""
echo "--- Case 2: プローズ判定不能 (plan_review.md 自体が無い) + 有効な structured_output.verdict → rescue が plan_review.md を新規作成し OK 扱い ---"
_run_case "TIMEOUT_NONE" "$RESULT_JSON_REVISE" 0 "true" "prose unreadable (no file at all) + valid structured output"

echo ""
echo "--- Case 3 (fail-closed 確認): プローズ判定不能 + ログが空/壊れている → rescue は発火せず既存どおり exit 1 ---"
_run_case "TIMEOUT_FRESH" "not valid json at all" 1 "false" "prose unreadable + broken log (no rescue, no fabricated verdict)"

# --- t012: 構造化出力を「プローズ失敗時の rescue」から「常に読む権威」へ ---
# $1 = wait_status, $2 = ログ内容, $3 = plan_review.md に書いておく本文
# ("" なら書かない), $4 = expect_rc, $5 = 期待する plan_review.md の1行目の
# verdict ("" なら「規定形式の1行目が無いこと」を期待), $6 = label,
# $7 = log_delay (秒。省略時0 = 同期書き。>0 の場合、mux_spawn は即座に
# 戻るが、ログの書き込みはバックグラウンドで指定秒数後に行う — F2 の
# 「構造化出力がプローズより後に遅れて届く」本番の非同期性を再現する)。
#
# plan_review.md は review-plan.sh 冒頭の `rm -f` より後に書かれる必要があるため、
# ログと同じく mux_spawn スタブの中で書く (mux_spawn は rm -f の後に呼ばれる)。
_run_case_ex() {
  local wait_status="$1" log_body="$2" review_body="$3" expect_rc="$4" expect_verdict="$5" label="$6" log_delay="${7:-0}"

  TMPDIR_TEST="/tmp/crewvia-test-review-plan-json-rescue-ex-$$-$(date +%s%N)"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission"

  cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
  cp "$SCRIPT_DIR/lib_verdict.py" "$TMPDIR_TEST/scripts/lib_verdict.py"
  cp "$REAL_SCHEMA" "$TMPDIR_TEST/config/plan-review-verdict.schema.json"
  cp "$REAL_LIB_MODEL" "$TMPDIR_TEST/scripts/lib_model.py"
  cp "$REAL_CREWVIA_YAML" "$TMPDIR_TEST/config/crewvia.yaml"

  local review_output="$TMPDIR_TEST/queue/missions/testmission/plan_review.md"
  if [[ "$log_delay" -gt 0 ]]; then
    cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  local self_pid="\$\$"
  ( sleep ${log_delay}; printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\${self_pid}.log" ) &
  disown 2>/dev/null || true
  if [[ -n "${review_body}" ]]; then
    printf '%s\n' '${review_body}' > "${review_output}"
  fi
  return 0
}
mux_kill() { return 0; }
EOF
  else
    cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  printf '%s\n' '${log_body}' > "/tmp/plan_reviewer_\$\$.log"
  if [[ -n "${review_body}" ]]; then
    printf '%s\n' '${review_body}' > "${review_output}"
  fi
  return 0
}
mux_kill() { return 0; }
EOF
  fi

  local wait_rc=0
  [[ "$wait_status" != "OK" ]] && wait_rc=1
  cat > "$TMPDIR_TEST/scripts/wait_for_plan_review.sh" << EOF
#!/usr/bin/env bash
echo "$wait_status"
exit $wait_rc
EOF
  chmod +x "$TMPDIR_TEST/scripts/wait_for_plan_review.sh"

  set +e
  bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_json_rescue_stdout 2>&1
  local rc=$?
  set -e

  local got=""
  if [[ -f "$review_output" ]]; then
    got="$(python3 "$TMPDIR_TEST/scripts/lib_verdict.py" "$review_output" 2>/dev/null || true)"
  fi

  if [[ "$rc" -eq "$expect_rc" && "$got" == "$expect_verdict" ]]; then
    pass "$label — exit=$rc, plan_review.md の判定='${got:-<none>}' (期待どおり)"
  else
    fail "$label — expected exit=$expect_rc verdict='${expect_verdict:-<none>}', got exit=$rc verdict='${got:-<none>}'. plan_review.md: $(cat "$review_output" 2>/dev/null || echo '<missing>')"
  fi
  rm -rf "$TMPDIR_TEST"
}

RESULT_JSON_APPROVE='{"type":"result","subtype":"success","is_error":false,"result":"{\"verdict\":\"approve\"}","structured_output":{"verdict":"approve"}}'

echo ""
echo "--- Case 5 (t012): プローズ成功 + 構造化出力が同じ値 → そのまま採用 (干渉しない) ---"
_run_case_ex "OK" "$RESULT_JSON_APPROVE" '**Verdict:** approve' 0 "approve" \
  "prose=approve / structured=approve → approve のまま"

echo ""
echo "--- Case 6 (t012 [最重要]): プローズ成功 + 構造化出力が食い違う → どちらも採らず判定不能 ---"
# 同じ plan-reviewer が plan_review.md と最終応答で違うことを言った場合、
# 判定は曖昧であり、危険な側 (approve) に倒してはならない。exit 1 にすると
# scripts/plan.sh が cycle を refund した上で Director に手動確認を促す。
_run_case_ex "OK" "$RESULT_JSON_REVISE" '**Verdict:** approve' 1 "approve" \
  "prose=approve / structured=revise → どちらも採らず exit 1 (plan_review.md は書き換えない)"

echo ""
echo "--- Case 7 (t012): プローズが書式を外した (判定不能) → 構造化出力が1行目に書き戻して回収 ---"
# t012 で lib_verdict を「1行目・完全一致」に絞ったため、書式を外した
# plan_review.md はすべて判定不能になる。その回収が効くことの確認。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" '# Plan Review: test

**Verdict:** approve' 0 "approve" \
  "1行目が判定行でない plan_review.md → 構造化出力から回収して approve"

echo ""
echo "--- Case 8 (t012 fail-closed): 構造化出力が無く、プローズも書式を外している → 判定不能のまま exit 1 ---"
_run_case_ex "TIMEOUT_FRESH" "no json here" '# Plan Review: test

**Verdict:** approve' 1 "" \
  "構造化出力なし + 1行目が判定行でない → verdict を捏造せず exit 1"

echo ""
echo "--- Case 4 (安全な結論はプローズ単独で確定してよい): プローズ=revise (真の判定行) + 構造化出力が一切無い → 待たずに revise のまま exit 0 ---"
_run_case_ex "OK" "" '**Verdict:** revise' 0 "revise" \
  "prose=revise / structured 皆無 → 待たずに revise のまま (安全な結論は確認不要)"

echo ""
echo "--- Case 9 (F2 コア回帰 — 本 PR の直接の動機): プローズ=approve + 構造化出力が最後まで確認できない → 確認なしに approve を通さない ---"
# 7642f9b (修正前) はここで WAIT_STATUS=="OK" (プローズ側は読めていた) の
# 一点だけを見て exit 0 のまま approve を通していた — 構造化出力による
# 確認を一度も試みていなかった。ログが永遠に来ないケースなので、
# ファイル冒頭で縮めた REVIEW_PLAN_STRUCTURED_MAX_WAIT により待ち切る
# ことそのものを確認する。
_run_case_ex "OK" "" '**Verdict:** approve' 1 "approve" \
  "prose=approve / structured 皆無 → 待っても確認できず exit 1 (plan_review.md 自体は書き換えない)"

echo ""
echo "--- Case 10 (F2 コア回帰・遅延到着): プローズ=approve が即座に読める一方、構造化出力 (=revise, 食い違い) が遅れて届く → 食い違いを検出して exit 1 ---"
# 7642f9b (修正前) は「プローズが既に読めていれば1回だけ非同期に読んで
# 終わり」だったため、この遅延書き込みを永遠に見逃し exit 0 のまま approve
# を通していた (memory: pr199-verdict-mechanism-handoff.md の
# log_late(delay=6) と同型)。修正後は log_delay 秒後に届く structured
# output にも追いつき、食い違いを検出できることを確認する。ファイル冒頭で
# 縮めたデフォルト上限 (2秒) では書き込み前に諦めてしまうため、このケース
# だけ余裕を持って上書きする (1秒遅延・5秒上限)。
REVIEW_PLAN_STRUCTURED_MAX_WAIT=5 _run_case_ex "OK" "$RESULT_JSON_REVISE" '**Verdict:** approve' 1 "approve" \
  "prose=approve (即時) / structured=revise (1秒遅延) → 遅延に追いつき食い違いを検出して exit 1" 1

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
