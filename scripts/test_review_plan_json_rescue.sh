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
# 待たずにプローズをそのまま採用していた。修正後は構造化出力の到着を
# (固定間隔ポーリング、最大 REVIEW_PLAN_STRUCTURED_MAX_WAIT 秒) 待ち、
# 確認できなければ approve を通さない。
#
# テスト方法: scripts/test_review_plan_pane_leak.sh と同じ手法 —
# scripts/review-plan.sh 本体を scratch ディレクトリにコピーし、
# lib_mux.sh (mux_spawn を no-op 成功にしつつ、$$ — review-plan.sh 自身の
# PID、source されたシェル関数なので $$ は呼び出し元と同じ — を使って
# PLAN_REVIEWER_LOG に claude --output-format json 相当の出力を模したログを
# 書く) と wait_for_plan_review.sh (プローズ解析の結果を模して即座に返す)
# をスタブに差し替える。claude CLI は一切起動しない。
#
# t015 (mission 20260912-verdict-ci-launcher, Director 設計判断3) 追記:
# 構造化出力が届かないケースでは review-plan.sh が明示的に mux_kill →
# mux_pid で停止確認をしてから plan_review.md を読む。このスタブは
# mux_pid を「常に見つからない (= 即座に停止確認できる)」に固定する。
#
# t018 (mission 20260912-verdict-ci-launcher, QA t016 Finn FAIL-A / Kai-codex
# t004 2 回目 P1, Director 設計判断1-4) 追記:
# review-plan.sh は lib_verdict.py の 3 状態 (有効 / 兆候なし / 書式違反) に
# 従い、構造化出力による救済を「兆候なし」のときだけに絞った。
# lib_verdict.py の終了コードは allowlist で解釈する (10 以外の「読めない」は
# すべて書式違反扱い)。Case 7 は挙動変更、Case 7b/13-18 を追加。
# さらに review-plan.sh が書く plan_review.verdict の形式
# ("<verdict>\nrun_id=<CREWVIA_PLAN_REVIEW_RUN_ID>\n") と、判定不能のときに
# plan_review.verdict を書かず plan_review.md も書き換えないことを
# _run_case_ex の中で毎回確認する。
#
# 検証内容:
#   1. プローズ判定不能 (兆候なし) + ログに有効な structured_output.verdict
#      → rescue が発火し、plan_review.md 冒頭に規定形式の行が書かれ exit 0
#   2. 同上 (plan_review.md が一度も書かれなかった場合でも rescue で新規作成)
#   3. プローズ判定不能 + ログが空/JSON として壊れている → exit 1
#   4. プローズ=revise + 構造化出力が一切無い → 待ち切った後 revise のまま exit 0
#   5〜6. t012 由来 (同じ値なら採用 / 食い違えば判定不能)
#   7. (t018 挙動変更) 1 行目が判定行でなく本文に規定形式の approve がある
#      → 兆候が 1 行目以外にある書式違反なので、structured=approve でも exit 1
#   7b. (t018) 兆候の無い別表記だけ → 救済は残る (exit 0 / approve)
#   8. 構造化出力が無く、プローズも書式を外している → exit 1
#   9. プローズ=approve + 構造化出力が最後まで確認できない → exit 1
#   10. プローズ=approve 即時 + 構造化出力=revise 遅延 → 食い違い検出 exit 1
#   11〜12. 構造化出力の envelope 自体が失敗している → 信頼しない
#   13. (t018) 1 行目 revise + 本文 approve (QA t016 B_k1) + structured=approve → exit 1
#   14〜17. (t018) lib_verdict.py が想定外の終了コード/出力を返す → 救済しない
#   18. (t018) 書式違反 + 構造化出力なし → exit 1
#
# t020 (mission 20260912-verdict-ci-launcher, PR #199 fix 4) 追記:
#   - ユーザー決定「approve は救済しない」: 兆候なし × structured=approve は
#     exit 0 + plan_review.verdict 無し (plan.sh が refund)。Case 7b は挙動変更、
#     Case 7c / 13b / 19 を追加。Case 14-17 は構造化出力を revise に変えた (期待値は同じ)
#   - スタブはログを review-plan.sh の PLAN_REVIEWER_LOG (mktemp) に書く
#   20. schema 不在 / 不正 / jq 不在でも停止確認後に 3 状態で分類し revise/reject を束縛
#   21. 起動時点のログが実行専用の新規ファイル (空・0600・旧パスでない)
#
# 実行: bash scripts/test_review_plan_json_rescue.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REVIEW_PLAN="${SCRIPT_DIR}/review-plan.sh"
REAL_LIB_VERDICT="${SCRIPT_DIR}/lib_verdict.py"
REAL_SCHEMA="${SCRIPT_DIR}/../config/plan-review-verdict.schema.json"
# t001 (mission 20260909-dead-config-sweep): review-plan.sh はモデル解決に
# scripts/lib_model.py と config/crewvia.yaml を必須で読むようになった
# (scripts/test_review_plan_director_identity.sh と同じ理由)。
REAL_LIB_MODEL="${SCRIPT_DIR}/lib_model.py"
REAL_CREWVIA_YAML="${SCRIPT_DIR}/../config/crewvia.yaml"

# review-plan.sh の構造化出力待ちループの poll 間隔・上限を、テスト実行を
# 速くするためだけに縮める (本番デフォルトは 3 秒 / 180 秒)。見つかった場合は
# 即座に抜けるので、成功系のケースには影響しない。
export REVIEW_PLAN_STRUCTURED_POLL_INTERVAL=1
export REVIEW_PLAN_STRUCTURED_MAX_WAIT=2

# t018: review-plan.sh は plan.sh から渡される実行ごとの識別子を
# plan_review.verdict の 2 行目に書く。
export CREWVIA_PLAN_REVIEW_RUN_ID="json-rescue-$$"

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
  # t020: スタブが記録した review-plan.sh の実行専用ログ (mktemp) を片付ける。
  if [[ -f "/tmp/test_review_plan_json_rescue_logs_$$" ]]; then
    while IFS= read -r _log; do
      [[ -n "$_log" ]] && rm -f "$_log"
    done < "/tmp/test_review_plan_json_rescue_logs_$$"
    rm -f "/tmp/test_review_plan_json_rescue_logs_$$"
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
  # t018: 以前はここで lib_verdict.py をコピーしておらず、python3 がスクリプト
  # 不在で返す rc=2 が「判定不能 = 救済可」として扱われていた。本物の
  # lib_verdict.py を置く (plan_review.md が無いので「兆候なし」になる)。
  cp "$REAL_LIB_VERDICT" "$TMPDIR_TEST/scripts/lib_verdict.py"
  cp "$REAL_SCHEMA" "$TMPDIR_TEST/config/plan-review-verdict.schema.json"
  cp "$REAL_LIB_MODEL" "$TMPDIR_TEST/scripts/lib_model.py"
  cp "$REAL_CREWVIA_YAML" "$TMPDIR_TEST/config/crewvia.yaml"

  # スタブ lib_mux.sh: mux_available/mux_spawn は常に成功 (no-op)。mux_spawn は
  # sourced function として review-plan.sh 自身のプロセスで実行されるため、
  # review-plan.sh のシェル変数 PLAN_REVIEWER_LOG をそのまま参照できる —
  # これを使って本物のログパスにログを書き込む (t020: ログは mktemp で作る
  # 実行専用のファイルになり、パスは推測できないため)。
  cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  echo "\$PLAN_REVIEWER_LOG" >> "/tmp/test_review_plan_json_rescue_logs_$$"
  if [[ -n "${log_body}" ]]; then
    printf '%s\n' '${log_body}' > "\$PLAN_REVIEWER_LOG"
  else
    : > "\$PLAN_REVIEWER_LOG"
  fi
  return 0
}
mux_kill() { return 0; }
mux_pid() { return 1; }
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
# ("" なら書かない), $4 = expect_rc, $5 = 期待する plan_review.md の判定
# (本物の lib_verdict.py で読んだ結果。"" なら「有効な判定なし」を期待),
# $6 = label, $7 = log_delay (秒。省略時0 = 同期書き。>0 の場合、mux_spawn は
# 即座に戻るが、ログの書き込みはバックグラウンドで指定秒数後に行う),
# $8 = lib_verdict.py の差し替え内容 (t018。省略時は本物をコピー。
# Python ソースを渡すとそれを scratch の lib_verdict.py として置く)。
#
# t018 で毎回追加で確認すること:
#   - exit 0 のとき: plan_review.verdict が "<期待値>\nrun_id=$CREWVIA_PLAN_REVIEW_RUN_ID\n" ちょうど
#   - exit 1 のとき: plan_review.verdict が存在しない、かつ plan_review.md が
#     スタブの書いた内容から書き換わっていない (救済の prepend が起きていない)
#
# plan_review.md は review-plan.sh 冒頭の `rm -f` より後に書かれる必要があるため、
# ログと同じく mux_spawn スタブの中で書く (mux_spawn は rm -f の後に呼ばれる)。
#
# t020 で追加した呼び出し側の変数 (関数呼び出しの前置代入で渡す):
#   CASE_SCHEMA_MODE   missing (schema を置かない) / invalid (壊れた JSON) /
#                      nojq (PATH 先頭の jq が exit 127)。省略時は本物の schema
#   CASE_MUX_PID_ALIVE 1 なら mux_pid が「まだ動いている」を返し続ける (停止確認不可)
#   CASE_EXPECT_BOUND  plan_review.verdict に束縛される値の期待。省略時は
#                      exit 0 なら $5 と同じ、exit 1 なら無し。"none" は「無し」
#   CASE_LOG_CHECK     1 ならスタブが起動時点のログファイルの状態を記録し、
#                      「新規・空・0600・旧パス (/tmp/plan_reviewer_$$.log) でない」を確認する
_run_case_ex() {
  local wait_status="$1" log_body="$2" review_body="$3" expect_rc="$4" expect_verdict="$5" label="$6" log_delay="${7:-0}" lib_stub="${8:-}"
  local schema_mode="${CASE_SCHEMA_MODE:-}" pid_alive="${CASE_MUX_PID_ALIVE:-0}" log_check="${CASE_LOG_CHECK:-0}"
  local expect_bound="${CASE_EXPECT_BOUND-__default__}"
  if [[ "$expect_bound" == "__default__" ]]; then
    expect_bound=""
    [[ "$expect_rc" -eq 0 ]] && expect_bound="$expect_verdict"
  elif [[ "$expect_bound" == "none" ]]; then
    expect_bound=""
  fi

  TMPDIR_TEST="/tmp/crewvia-test-review-plan-json-rescue-ex-$$-$(date +%s%N)"
  rm -rf "$TMPDIR_TEST"
  mkdir -p "$TMPDIR_TEST/scripts" "$TMPDIR_TEST/config" "$TMPDIR_TEST/queue/missions/testmission" "$TMPDIR_TEST/stubbin"

  cp "$REAL_REVIEW_PLAN" "$TMPDIR_TEST/scripts/review-plan.sh"
  if [[ -n "$lib_stub" ]]; then
    printf '%s\n' "$lib_stub" > "$TMPDIR_TEST/scripts/lib_verdict.py"
  else
    cp "$REAL_LIB_VERDICT" "$TMPDIR_TEST/scripts/lib_verdict.py"
  fi
  case "$schema_mode" in
    missing) ;;
    invalid) printf '{"type": "object", "properties": \n' > "$TMPDIR_TEST/config/plan-review-verdict.schema.json" ;;
    *) cp "$REAL_SCHEMA" "$TMPDIR_TEST/config/plan-review-verdict.schema.json" ;;
  esac
  local path_prefix=""
  if [[ "$schema_mode" == "nojq" ]]; then
    printf '#!/usr/bin/env bash\nexit 127\n' > "$TMPDIR_TEST/stubbin/jq"
    chmod +x "$TMPDIR_TEST/stubbin/jq"
    path_prefix="$TMPDIR_TEST/stubbin:"
  fi
  cp "$REAL_LIB_MODEL" "$TMPDIR_TEST/scripts/lib_model.py"
  cp "$REAL_CREWVIA_YAML" "$TMPDIR_TEST/config/crewvia.yaml"

  local review_output="$TMPDIR_TEST/queue/missions/testmission/plan_review.md"
  local verdict_file="$TMPDIR_TEST/queue/missions/testmission/plan_review.verdict"
  local log_state="$TMPDIR_TEST/log_state"
  local mux_pid_body="return 1;"
  [[ "$pid_alive" == "1" ]] && mux_pid_body="echo 999999; return 0;"
  if [[ "$log_delay" -gt 0 ]]; then
    cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  echo "\$PLAN_REVIEWER_LOG" >> "/tmp/test_review_plan_json_rescue_logs_$$"
  ( sleep ${log_delay}; printf '%s\n' '${log_body}' > "\$PLAN_REVIEWER_LOG" ) &
  disown 2>/dev/null || true
  if [[ -n "${review_body}" ]]; then
    printf '%s\n' '${review_body}' > "${review_output}"
  fi
  return 0
}
mux_kill() { return 0; }
mux_pid() { ${mux_pid_body} }
EOF
  else
    cat > "$TMPDIR_TEST/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  echo "\$PLAN_REVIEWER_LOG" >> "/tmp/test_review_plan_json_rescue_logs_$$"
  if [[ "${log_check}" == "1" ]]; then
    python3 -c 'import os, stat, sys; s = os.lstat(sys.argv[1]); print(oct(stat.S_IMODE(s.st_mode)), s.st_size, stat.S_ISREG(s.st_mode), sys.argv[1] == sys.argv[2])' \
      "\$PLAN_REVIEWER_LOG" "/tmp/plan_reviewer_\$\$.log" > "${log_state}" 2>&1
  fi
  printf '%s\n' '${log_body}' > "\$PLAN_REVIEWER_LOG"
  if [[ -n "${review_body}" ]]; then
    printf '%s\n' '${review_body}' > "${review_output}"
  fi
  return 0
}
mux_kill() { return 0; }
mux_pid() { ${mux_pid_body} }
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
  PATH="${path_prefix}$PATH" bash "$TMPDIR_TEST/scripts/review-plan.sh" testmission >/tmp/test_review_plan_json_rescue_stdout 2>&1
  local rc=$?
  set -e

  # 判定は常に本物の lib_verdict.py で読む (差し替えたスタブに引きずられないため)。
  local got=""
  if [[ -f "$review_output" ]]; then
    got="$(python3 "$REAL_LIB_VERDICT" "$review_output" 2>/dev/null || true)"
  fi

  local problems=""
  [[ "$rc" -eq "$expect_rc" ]] || problems+="exit=$rc (expected $expect_rc); "
  [[ "$got" == "$expect_verdict" ]] || problems+="plan_review.md verdict='${got:-<none>}' (expected '${expect_verdict:-<none>}'); "
  if [[ -n "$expect_bound" ]]; then
    if ! python3 -c 'import sys; sys.exit(0 if open(sys.argv[1], encoding="utf-8", newline="").read() == f"{sys.argv[2]}\nrun_id={sys.argv[3]}\n" else 1)' \
        "$verdict_file" "$expect_bound" "$CREWVIA_PLAN_REVIEW_RUN_ID" 2>/dev/null; then
      problems+="plan_review.verdict is not exactly '${expect_bound}\\nrun_id=${CREWVIA_PLAN_REVIEW_RUN_ID}\\n' (got: $(od -c "$verdict_file" 2>/dev/null | head -3 | tr '\n' ' ' || echo '<missing>')); "
    fi
  else
    # 拒否 (exit 1) または確定しなかった (t020: exit 0 + plan_review.verdict 無し)。
    [[ ! -e "$verdict_file" ]] || problems+="plan_review.verdict was written on a refusal path ($(tr '\n' '|' < "$verdict_file")); "
    if [[ -n "$review_body" ]]; then
      [[ "$(cat "$review_output" 2>/dev/null)" == "$review_body" ]] || problems+="plan_review.md was rewritten on a refusal path; "
    else
      [[ ! -e "$review_output" ]] || problems+="plan_review.md was created on a refusal path; "
    fi
  fi
  if [[ "$log_check" == "1" ]]; then
    local state
    state="$(cat "$log_state" 2>/dev/null)"
    [[ "$state" == "0o600 0 True False" ]] || problems+="log file at launch was not a fresh private file (mode size is_regular is_old_path = '${state:-<not recorded>}', expected '0o600 0 True False'); "
  fi

  if [[ -z "$problems" ]]; then
    pass "$label — exit=$rc, plan_review.md の判定='${got:-<none>}' (期待どおり)"
  else
    fail "$label — ${problems}plan_review.md: $(cat "$review_output" 2>/dev/null || echo '<missing>') / log: $(cat /tmp/test_review_plan_json_rescue_stdout)"
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
_run_case_ex "OK" "$RESULT_JSON_REVISE" '**Verdict:** approve' 1 "approve" \
  "prose=approve / structured=revise → どちらも採らず exit 1 (plan_review.md は書き換えない)"

echo ""
echo "--- Case 7 (t018 挙動変更): 1行目が判定行でなく、本文に規定形式の approve がある → 書式違反として救済しない ---"
# t012〜t015 はこの入力を「判定不能」として構造化出力から回収していた (exit 0)。
# t018 (Director 設計判断2) で「兆候が 1 行目以外にある」は書式違反になり、
# 構造化出力の値に関係なく fail-closed。本物の lib_verdict.py で読むと
# 書式違反なので判定は '' (<none>)。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" '# Plan Review: test

**Verdict:** approve' 1 "" \
  "1行目が判定行でない plan_review.md + structured=approve → 書式違反として exit 1 (救済しない)"

echo ""
echo "--- Case 7b (t020 挙動変更): verdict 行の兆候が無い別表記 + structured=approve → approve は救済しない ---"
# t018 ではこの入力を 1 行目に approve を書き戻して exit 0 / plan_review.verdict=approve
# にしていた。ユーザー決定により approve の救済を廃止 (t020)。exit 0 +
# plan_review.verdict 無し (plan.sh は plan_review.md の有無に関係なく refund) で、
# plan_review.md も書き換えない。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" '# Plan Review: test

## 総合判定: GO' 0 "" \
  "兆候なしの別表記 (## 総合判定: GO) + structured=approve → 書き戻さず exit 0、plan_review.verdict 無し (refund)"

echo ""
echo "--- Case 7c (t020): verdict 行の兆候が無い別表記 + structured=revise → 救済は revise / reject に限って残る ---"
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '# Plan Review: test

## 総合判定: 要修正' 0 "revise" \
  "兆候なしの別表記 + structured=revise → 1行目に書き戻して revise"

echo ""
echo "--- Case 8 (t012 fail-closed): 構造化出力が無く、プローズも書式を外している → 判定不能のまま exit 1 ---"
_run_case_ex "TIMEOUT_FRESH" "no json here" '# Plan Review: test

**Verdict:** approve' 1 "" \
  "構造化出力なし + 1行目が判定行でない → verdict を捏造せず exit 1"

echo ""
echo "--- Case 4 (安全な結論は待った上で確定してよい): プローズ=revise (真の判定行) + 構造化出力が一切無い → 待ち切った後 revise のまま exit 0 ---"
# t015 (Director 設計判断2) で挙動変更: revise/reject でも approve と同じ
# MAX_WAIT だけ構造化出力の到着を待ってから確定する。
_run_case_ex "OK" "" '**Verdict:** revise' 0 "revise" \
  "prose=revise / structured 皆無 → 同じ MAX_WAIT だけ待った上で revise を採用"

echo ""
echo "--- Case 9 (F2 コア回帰 — 本 PR の直接の動機): プローズ=approve + 構造化出力が最後まで確認できない → 確認なしに approve を通さない ---"
_run_case_ex "OK" "" '**Verdict:** approve' 1 "approve" \
  "prose=approve / structured 皆無 → 待っても確認できず exit 1 (plan_review.md 自体は書き換えない)"

echo ""
echo "--- Case 11 (Codex P2, t015): 構造化出力の envelope が失敗している (is_error:true) → verdict を信頼しない ---"
RESULT_JSON_ERROR_ENVELOPE='{"type":"result","subtype":"error_during_execution","is_error":true,"structured_output":{"verdict":"approve"}}'
_run_case_ex "OK" "$RESULT_JSON_ERROR_ENVELOPE" '**Verdict:** approve' 1 "approve" \
  "structured envelope が is_error:true (実行失敗) → verdict を信頼せず exit 1 (plan_review.md は書き換えない)"

echo ""
echo "--- Case 12 (Codex P2, t015): subtype が success 以外 (is_error は false) → verdict を信頼しない ---"
RESULT_JSON_BAD_SUBTYPE='{"type":"result","subtype":"error_max_turns","is_error":false,"structured_output":{"verdict":"approve"}}'
_run_case_ex "OK" "$RESULT_JSON_BAD_SUBTYPE" '**Verdict:** approve' 1 "approve" \
  "structured envelope の subtype が success でない → verdict を信頼せず exit 1"

echo ""
echo "--- Case 10 (F2 コア回帰・遅延到着): プローズ=approve が即座に読める一方、構造化出力 (=revise, 食い違い) が遅れて届く → 食い違いを検出して exit 1 ---"
REVIEW_PLAN_STRUCTURED_MAX_WAIT=5 _run_case_ex "OK" "$RESULT_JSON_REVISE" '**Verdict:** approve' 1 "approve" \
  "prose=approve (即時) / structured=revise (1秒遅延) → 遅延に追いつき食い違いを検出して exit 1" 1

echo ""
echo "--- Case 13 (t018, QA t016 B_k1 / Kai-codex P1): 1行目 revise + 本文に approve + structured=approve → 救済せず exit 1 ---"
# b21d4b4 (t015) では lib_verdict が自己矛盾を None (判定不能) に丸め、
# structured=approve で救済して exit 0 / plan_review.verdict=approve になっていた。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" '**Verdict:** revise

書式例として:

**Verdict:** approve' 1 "" \
  "自己矛盾 (revise + 本文 approve) + structured=approve → exit 1、plan_review.verdict も plan_review.md の書き換えも無し"

echo ""
echo "--- Case 13b (t018 の不変条件を revise でも確認): 1行目 revise + 本文に approve + structured=revise → 救済せず exit 1 ---"
# t020 で approve の救済自体を廃止したため、structured=approve の Case 13 は
# 「兆候があれば救済しない」が壊れても exit 1 のままになる。revise でも救済しないことを別に確認する。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '**Verdict:** approve

書式例として:

**Verdict:** revise' 1 "" \
  "自己矛盾 (approve + 本文 revise) + structured=revise → exit 1、plan_review.verdict も plan_review.md の書き換えも無し"

echo ""
echo "--- Case 14-17 (t018): lib_verdict.py が想定外の結果を返したら救済しない (終了コードの allowlist) ---"
# プローズは兆候の無い別表記 (本物の lib_verdict.py なら救済される入力) にし、
# 差し替えたスタブの結果だけで exit 1 に倒れることを確認する。
# t020: 構造化出力を approve から revise に変えた (期待値は同じ)。approve は t020 で
# no_sign でも救済されなくなったため、approve のままだと allowlist が壊れても exit 1 に
# なり、このケースが何も検証しなくなる。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '## 総合判定: GO' 1 "" \
  "lib_verdict.py が落ちた (未捕捉例外 = rc 1) + structured=revise → exit 1" 0 \
  $'import sys\nsys.exit(1)'
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '## 総合判定: GO' 1 "" \
  "lib_verdict.py が rc 2 (python3 がスクリプトを開けないときと同じ) + structured=revise → exit 1" 0 \
  $'import sys\nsys.exit(2)'
# t020 (t019 軽微指摘): rc 2 の拒否理由が「verdict-line sign を含む」ではなく実態を言うこと。
if grep -q 'lib_verdict.py could not be run or failed (rc=2)' /tmp/test_review_plan_json_rescue_stdout \
    && ! grep -q 'contains a verdict-line sign' /tmp/test_review_plan_json_rescue_stdout; then
  pass "lib_verdict.py rc 2 の拒否理由は「lib_verdict.py を実行できなかった」(verdict-line sign とは言わない)"
else
  fail "lib_verdict.py rc 2 の拒否理由が実態と合わない: $(grep 'review-plan.sh' /tmp/test_review_plan_json_rescue_stdout | head -3)"
fi
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '## 総合判定: GO' 1 "" \
  "lib_verdict.py が rc 0 で正規でない出力 ('APPROVE') + structured=revise → exit 1" 0 \
  'print("APPROVE")'
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_REVISE" '## 総合判定: GO' 1 "" \
  "lib_verdict.py が rc 10 (兆候なし) なのに出力がある + structured=revise → exit 1" 0 \
  $'import sys\nprint("approve")\nsys.exit(10)'

echo ""
echo "--- Case 18 (t018): 書式違反 (**Verdict:** REVISE) + 構造化出力なし → exit 1 ---"
_run_case_ex "TIMEOUT_FRESH" "no json here" '**Verdict:** REVISE' 1 "" \
  "書式違反 + structured 皆無 → exit 1"

echo ""
echo "--- Case 19 (t020, QA t019 残余 / ユーザー決定): 兆候の定義の外の revise 表記 + structured=approve → approve を束縛しない ---"
# いずれも lib_verdict.py では NO_SIGN (兆候なし)。t018 までは structured=approve で救済されていた。
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" '判定: revise' 0 "" \
  "'判定: revise' + structured=approve → exit 0、plan_review.verdict 無し、書き換え無し"
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" $'**Verdict∶** revise' 0 "" \
  "'**Verdict∶** revise' (U+2236) + structured=approve → exit 0、plan_review.verdict 無し、書き換え無し"
_run_case_ex "TIMEOUT_FRESH" "$RESULT_JSON_APPROVE" $'**Vеrdict:** revise' 0 "" \
  "'**Vеrdict:** revise' (キリル文字の е) + structured=approve → exit 0、plan_review.verdict 無し、書き換え無し"
# plan_review.md が無い場合も exit 0 (exit 1 だと plan.sh は plan_review.md が無いときに refund しない)。
_run_case_ex "TIMEOUT_NONE" "$RESULT_JSON_APPROVE" '' 0 "" \
  "plan_review.md 未作成 + structured=approve → exit 0、plan_review.md を作らず plan_review.verdict 無し"

echo ""
echo "--- Case 20 (t020, Kai-codex 3 回目 #1): schema 不在 / 不正 / jq 不在でも、停止確認後にプローズを 3 状態で分類する ---"
CASE_SCHEMA_MODE=missing _run_case_ex "OK" "" '**Verdict:** revise' 0 "revise" \
  "schema 不在 + 1行目 revise → revise を束縛して exit 0"
CASE_SCHEMA_MODE=invalid _run_case_ex "OK" "" '**Verdict:** reject' 0 "reject" \
  "schema が不正な JSON + 1行目 reject → reject を束縛して exit 0"
CASE_SCHEMA_MODE=nojq _run_case_ex "OK" "" '**Verdict:** revise' 0 "revise" \
  "jq を実行できない + 1行目 revise → revise を束縛して exit 0"
CASE_SCHEMA_MODE=missing _run_case_ex "OK" "" '**Verdict:** approve' 1 "approve" \
  "schema 不在 + 1行目 approve → 構造化出力が無いので approve は成立しない (exit 1、束縛無し)"
CASE_SCHEMA_MODE=missing _run_case_ex "TIMEOUT_FRESH" "" $'**Verdict:** revise\n\n**Verdict:** approve' 1 "" \
  "schema 不在 + 自己矛盾 → 書式違反として exit 1"
CASE_SCHEMA_MODE=missing CASE_MUX_PID_ALIVE=1 CASE_EXPECT_BOUND=none _run_case_ex "OK" "" '**Verdict:** revise' 0 "revise" \
  "schema 不在 + reviewer の停止を確認できない → plan_review.md を読まず束縛しない (t015 設計判断 3)"

echo ""
echo "--- Case 21 (t020, Kai-codex 3 回目 #3): reviewer 起動時点のログは実行専用の新規ファイル (空・0600・旧パスでない) ---"
CASE_LOG_CHECK=1 _run_case_ex "OK" "$RESULT_JSON_REVISE" '**Verdict:** revise' 0 "revise" \
  "起動時のログが空・0600・/tmp/plan_reviewer_\$\$.log でない + revise の正常系は変わらない"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
