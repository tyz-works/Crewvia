#!/usr/bin/env bash
# scripts/test_review_plan_verdict_binding_e2e.sh
# t015 / t018 (mission 20260912-verdict-ci-launcher) の e2e 回帰テスト。
#
# 実物の `scripts/plan.sh review` → `scripts/review-plan.sh` →
# `scripts/wait_for_plan_review.sh` / `scripts/lib_verdict.py` を通す
# (QA t003/t016 Finn のハーネスと同じ設計)。差し替えるのは scratch 側の
# scripts/lib_mux.sh と、PATH 先頭の claude / herdr / tmux スタブだけ:
#   - mux_spawn: reviewer の代わりに plan_review.md と PLAN_REVIEWER_LOG を
#     書く (log_delay > 0 ならログはバックグラウンドで遅れて書く)。
#     呼ばれた印 (stub_mux_spawn_called) を残す。
#   - mux_kill: 呼ばれたその場で (同期的に) plan_review.md を書き換える —
#     herdr の tab close が効くまでの間に reviewer がもう 1 回 Write する、
#     という本番の窓を決定論的に再現する。
#   - mux_pid: 書き換えが完了済みなら「見つからない (=停止確認できる)」を返す。
#   - claude / herdr / tmux: 呼ばれたら記録して exit 97。1 回でも呼ばれたら FAIL。
# 各ケースで「スタブ mux_spawn が呼ばれた」「実物/スタブのバイナリが呼ばれて
# いない」を確認し、さらに全ケースの前に SANITY ケースが通ることを確認する
# (通らなければ以降を実行せず exit 1)。
#
# 本体スイート (現在の作業ツリーのコード):
#   - t015: T1/T1r (TOCTOU) / F2c / F2g / E1 (envelope) / Decision1 (束縛)
#   - t018 (QA t016 Finn FAIL-A / Kai-codex P1, Director 設計判断1-4):
#     B_k1〜B_k5 × structured=approve / blockquote・インデント・リスト・フェンス内の
#     approve 引用 + 1 行目 revise / 同じ値の重複 / 太字なし / 不正 UTF-8
#     → いずれも ready にならず refund (cycle_count=0)。
#     兆候なしの別表記 + structured=approve → ready (救済は残る)。
#
# --verify-red (t018, QA t016 Finn FAIL-B / Kai-codex P2 で作り直し):
#   t015 版はここで `git show HEAD:...` を読んでいたため、修正版を commit した
#   後は修正版そのものを「旧版」として実行していた。さらに本体スイートの
#   PASS/FAIL カウンタを 0 にリセットしていたため、本体の失敗があっても
#   exit 0 になっていた (Finn G2)。作り直した内容:
#     1. 脆弱版を commit sha で固定し、`git archive <sha>` で scripts/ と config/
#        を丸ごと取り出して実行する (現在のツリーのファイルを混ぜない)。
#        実行したファイルの blob id を表示し、固定した sha の blob id と一致
#        しなければ FAIL にする。
#          3388334 (t002): T1 / T1r (TOCTOU) / E1 (envelope) / CR (lib_verdict CLI)
#          b21d4b4 (t015): B_k1〜B_k5 (自己矛盾・書式違反の救済)
#     2. 期待するのは「脆弱な結果 (status=ready / verdict=approve) が再現する
#        こと」。再現しなければ red 確認そのものを FAIL にする。
#     3. red のカウンタは本体スイートと別に持ち、本体をリセットしない。
#        終了コードは「本体の失敗 0 件 かつ red の未再現 0 件」のときだけ 0。
#     4. G2 のメタテスト: 作業ツリーのコピーに 3388334 の review-plan.sh と
#        plan.sh を置いてこのスクリプト自身を --verify-red で実行し、
#        本体の失敗が非 0 終了に反映されることを確認する。
#
# 環境変数 (主にメタテスト用):
#   BINDING_E2E_GIT_REPO     脆弱版を取り出す git リポジトリ (既定: このスクリプトのリポジトリ)
#   BINDING_E2E_CASE_FILTER  ラベルに対する正規表現。指定時は一致するケースだけ実行
#   BINDING_E2E_SKIP_META=1  G2 メタテストを実行しない
#
# 実行: bash scripts/test_review_plan_verdict_binding_e2e.sh
#       bash scripts/test_review_plan_verdict_binding_e2e.sh --verify-red

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GIT_REPO="${BINDING_E2E_GIT_REPO:-$OWN_CHECKOUT_ROOT}"
CASE_FILTER="${BINDING_E2E_CASE_FILTER:-}"

VERIFY_RED=0
[[ "${1:-}" == "--verify-red" ]] && VERIFY_RED=1

# 脆弱版として固定する commit (PR #199 の履歴上の実在 commit)。
RED_SHA_T002="3388334"   # t002: TOCTOU / envelope / CR が未修正
RED_SHA_T015="b21d4b4"   # t015: 自己矛盾・書式違反を構造化出力で救済してしまう

# 本番の mux / queue / Taskvia に触れないための保険 (呼び出し側でも env -u すること)。
unset CREWVIA_MUX CREWVIA_MUX_ENABLED CREWVIA_TMUX HERDR_ENV TMUX CREWVIA_REPO_ROOT \
      TASKVIA_URL TASKVIA_TOKEN 2>/dev/null || true

# 実行を速くするためだけに、構造化出力待ちの上限を縮める (本番デフォルトは
# 3秒/180秒)。見つかった場合は即座に抜けるので正常系には影響しない。
export REVIEW_PLAN_STRUCTURED_POLL_INTERVAL=1
export REVIEW_PLAN_STRUCTURED_MAX_WAIT=2
export REVIEW_PLAN_STOP_CONFIRM_POLL_INTERVAL=1
export REVIEW_PLAN_STOP_CONFIRM_MAX_SECONDS=2

PASS_COUNT=0
FAIL_COUNT=0
RED_PASS_COUNT=0
RED_FAIL_COUNT=0
COUNTER_GROUP="main"
pass() {
  if [[ "$COUNTER_GROUP" == "red" ]]; then RED_PASS_COUNT=$((RED_PASS_COUNT + 1)); else PASS_COUNT=$((PASS_COUNT + 1)); fi
  echo "  PASS: $1"
}
fail() {
  if [[ "$COUNTER_GROUP" == "red" ]]; then RED_FAIL_COUNT=$((RED_FAIL_COUNT + 1)); else FAIL_COUNT=$((FAIL_COUNT + 1)); fi
  echo "  FAIL: $1"
}

# 実行するコードの出どころ。SRC_ROOT/scripts/plan.sh を起動し、
# SRC_ROOT/scripts/* と SRC_ROOT/config/* を scratch にコピーする。
# PINNED_SHA が空でなければ、実行したファイルの blob id をその sha と照合する。
SRC_ROOT="$OWN_CHECKOUT_ROOT"
PINNED_SHA=""

echo "== test_review_plan_verdict_binding_e2e.sh (t015 T1/T1r TOCTOU + t018 FAIL-A 自己矛盾の救済 / FAIL-B red ハーネス) =="

_cleanup_dir() {
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$1" 2>/dev/null || true
}

_selected() {
  [[ -z "$CASE_FILTER" || "$1" =~ $CASE_FILTER ]]
}

# $1 = label, $2 = initial_prose (mux_spawn が書く plan_review.md の本文),
# $3 = rewrite_prose ("" なら mux_kill は何もしない), $4 = log_body
# ("" なら構造化出力は一切出さない), $5 = log_delay_seconds,
# $6 = expect_status (ready|drafting), $7 = expect_verdict ("" は null 期待)。
# cycle_count は verdict が null なら 0 (refund)、それ以外は 1 を期待する。
_run_case() {
  local label="$1" initial_prose="$2" rewrite_prose="$3" log_body="$4" \
        log_delay="$5" expect_status="$6" expect_verdict="$7"
  _selected "$label" || return 0

  local src="$SRC_ROOT"
  local plan_sh="$src/scripts/plan.sh"
  local T="/tmp/crewvia-test-verdict-binding-e2e-$$-$(date +%s%N)"
  _cleanup_dir "$T"
  mkdir -p "$T/scripts" "$T/config" "$T/stubbin"
  export CREWVIA_QUEUE="$T/queue"

  local f
  for f in lint_plan.py lib_verdict.py lib_model.py wait_for_plan_review.sh review-plan.sh; do
    if ! cp "$src/scripts/$f" "$T/scripts/"; then
      fail "$label — setup: could not copy scripts/$f from $src"
      _cleanup_dir "$T"
      return 0
    fi
  done
  cp -r "$src/config/." "$T/config/"
  "$plan_sh" init "Test Mission" --mission testmission >/dev/null 2>&1

  local mission_dir="$T/queue/missions/testmission"
  local review_output="$mission_dir/plan_review.md"
  local verdict_file="$mission_dir/plan_review.verdict"
  local rewrite_flag="$T/rewrite_done.flag"
  local spawn_marker="$T/stub_mux_spawn_called"
  local binary_marker="$T/stub_binary_called"
  local pid_file="$T/review_plan_pid"

  printf '%s\n' "$initial_prose" > "$T/initial_prose"
  [[ -n "$rewrite_prose" ]] && printf '%s\n' "$rewrite_prose" > "$T/rewrite_prose"
  [[ -n "$log_body" ]] && printf '%s\n' "$log_body" > "$T/log_body"

  local b
  for b in claude herdr tmux; do
    printf '#!/usr/bin/env bash\necho "%s $*" >> "%s"\nexit 97\n' "$b" "$binary_marker" > "$T/stubbin/$b"
    chmod +x "$T/stubbin/$b"
  done

  # スタブ lib_mux.sh。mux_* は sourced function として review-plan.sh 自身の
  # プロセスで実行されるため、中の $$ は review-plan.sh の PID
  # (= PLAN_REVIEWER_LOG の実際のパス) と一致する。
  cat > "$T/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() {
  touch "${spawn_marker}"
  echo "\$\$" > "${pid_file}"
  cp "${T}/initial_prose" "${review_output}.stubtmp" && mv "${review_output}.stubtmp" "${review_output}"
  if [[ -f "${T}/log_body" ]]; then
    if [[ "${log_delay}" -gt 0 ]]; then
      ( sleep ${log_delay}; cp "${T}/log_body" "/tmp/plan_reviewer_\$\$.log.stubtmp" && mv "/tmp/plan_reviewer_\$\$.log.stubtmp" "/tmp/plan_reviewer_\$\$.log" ) &
      disown 2>/dev/null || true
    else
      cp "${T}/log_body" "/tmp/plan_reviewer_\$\$.log"
    fi
  fi
  return 0
}
mux_kill() {
  # kill が返る前に、herdr の SIGHUP 猶予 (最大2秒) の間に起きうる reviewer の
  # もう1回の Write を同期的に完了させてから返す。
  if [[ -f "${T}/rewrite_prose" ]]; then
    cp "${T}/rewrite_prose" "${review_output}"
  fi
  touch "${rewrite_flag}"
  return 0
}
mux_pid() {
  if [[ -f "${rewrite_flag}" ]]; then
    return 1
  fi
  echo 999999
  return 0
}
EOF

  local out rc=0
  out="$(PATH="$T/stubbin:$PATH" "$plan_sh" review testmission 2>&1)" || rc=$?

  local status verdict cycle got_verdict_file="<none>"
  status="$(awk '/^status:/ { print $2; exit }' "$mission_dir/mission.yaml" 2>/dev/null)"
  verdict="$(awk '/^  last_verdict:/ { print $2; exit }' "$mission_dir/mission.yaml" 2>/dev/null)"
  cycle="$(awk '/^  cycle_count:/ { print $2; exit }' "$mission_dir/mission.yaml" 2>/dev/null)"
  [[ -f "$verdict_file" ]] && got_verdict_file="$(tr '\n' '|' < "$verdict_file")"

  local problems=()
  [[ -f "$spawn_marker" ]] || problems+=("stub mux_spawn was NOT called — the pipeline was not exercised")
  [[ -s "$binary_marker" ]] && problems+=("a claude/herdr/tmux binary was invoked: $(tr '\n' ';' < "$binary_marker")")
  [[ "$status" == "$expect_status" ]] || problems+=("status=$status (expected $expect_status)")
  if [[ -z "$expect_verdict" ]]; then
    [[ "$verdict" == "null" || -z "$verdict" ]] || problems+=("last_verdict=$verdict (expected null)")
  else
    [[ "$verdict" == "$expect_verdict" ]] || problems+=("last_verdict=$verdict (expected $expect_verdict)")
  fi
  local expect_cycle=1
  [[ -z "$expect_verdict" ]] && expect_cycle=0
  [[ "$cycle" == "$expect_cycle" ]] || problems+=("cycle_count=$cycle (expected $expect_cycle)")

  if [[ -n "$PINNED_SHA" ]]; then
    local pair path executed want got under_test
    for pair in \
        "scripts/plan.sh|$plan_sh" \
        "scripts/lib_verdict.py|$src/scripts/lib_verdict.py" \
        "scripts/review-plan.sh|$T/scripts/review-plan.sh" \
        "scripts/wait_for_plan_review.sh|$T/scripts/wait_for_plan_review.sh" \
        "scripts/lib_verdict.py|$T/scripts/lib_verdict.py"; do
      path="${pair%%|*}"
      executed="${pair#*|}"
      want="$(git -C "$GIT_REPO" rev-parse "$PINNED_SHA:$path" 2>/dev/null)"
      got="$(git -C "$GIT_REPO" hash-object --no-filters "$executed" 2>/dev/null)"
      under_test="$(git -C "$GIT_REPO" hash-object --no-filters "$OWN_CHECKOUT_ROOT/$path" 2>/dev/null)"
      echo "    executed ${executed#/tmp/} blob=${got:-?} | $PINNED_SHA:$path=${want:-?} | tree under test=${under_test:-?}"
      [[ -n "$want" && "$got" == "$want" ]] || problems+=("executed $path does not match $PINNED_SHA")
    done
  fi

  if [[ "${#problems[@]}" -eq 0 ]]; then
    pass "$label — status=$status verdict=$verdict cycle_count=$cycle (plan_review.verdict=$got_verdict_file)"
  else
    local joined
    joined="$(printf '%s; ' "${problems[@]}")"
    fail "$label — ${joined}plan.sh review output: $out"
  fi

  if [[ -f "$pid_file" ]]; then
    rm -f "/tmp/plan_reviewer_$(cat "$pid_file").log"
  fi
  _cleanup_dir "$T"
  unset CREWVIA_QUEUE
  return 0
}

RESULT_JSON_APPROVE='{"type":"result","subtype":"success","is_error":false,"structured_output":{"verdict":"approve"}}'
RESULT_JSON_REVISE='{"type":"result","subtype":"success","is_error":false,"structured_output":{"verdict":"revise"}}'
RESULT_JSON_ERROR_APPROVE='{"type":"result","subtype":"error_during_execution","is_error":true,"structured_output":{"verdict":"approve"}}'

PROSE_K1=$'**Verdict:** revise\n\n# Plan Review: testmission\n\n書式例として:\n\n**Verdict:** approve'
PROSE_K2=$'**Verdict:** approve\n\n# Plan Review: testmission\n\n**Verdict:** revise'
PROSE_K3='**Verdict:** revise (重大な指摘あり)'
PROSE_K4='**Verdict:** REVISE'
PROSE_K5=$'**Verdict:** reject\n\n# Plan Review: testmission\n\n**Verdict:** approve'

echo ""
echo "--- SANITY: スタブ経由でパイプラインが動くこと (prose=approve + structured=approve → ready/approve) ---"
SANITY_FAIL_BEFORE="$FAIL_COUNT"
CASE_FILTER_SAVED="$CASE_FILTER"
CASE_FILTER=""
_run_case "SANITY: approve + structured=approve (即時) → ready/approve" \
  '**Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
CASE_FILTER="$CASE_FILTER_SAVED"
if [[ "$FAIL_COUNT" -ne "$SANITY_FAIL_BEFORE" ]]; then
  echo "  ABORT: sanity case failed — the stubbed pipeline is not working, refusing to run the real cases"
  exit 1
fi

echo ""
echo "--- T1 (t015 FAIL-1, Kai-codex P1): prose=revise で検証 → kill 中に reviewer が approve へ再 Write, structured 無し ---"
_run_case "T1 (revise → kill 中に approve へ再Write, structured無し)" \
  '**Verdict:** revise' '**Verdict:** approve' '' 0 "drafting" ""

echo ""
echo "--- T1r (FAIL-1 亜種): prose=reject で検証 → kill 中に approve へ再 Write ---"
_run_case "T1r (reject → kill 中に approve へ再Write, structured無し)" \
  '**Verdict:** reject' '**Verdict:** approve' '' 0 "drafting" ""

echo ""
echo "--- 対照: prose=revise, 書き換えなし, structured 無し → revise として消費 ---"
_run_case "対照: revise, 書き換えなし, structured無し → drafting/revise" \
  '**Verdict:** revise' '' '' 0 "drafting" "revise"

echo ""
echo "--- F2c (正常系): prose=approve, structured=approve が遅延到着 → ready/approve ---"
_run_case "F2c: approve + structured=approve (1秒遅延) → ready/approve" \
  '**Verdict:** approve' '' "$RESULT_JSON_APPROVE" 1 "ready" "approve"

echo ""
echo "--- F2g: prose=revise, structured=approve が遅延到着 → 食い違いとして fail-closed ---"
_run_case "F2g: revise(prose) + structured=approve (1秒遅延) → drafting/null (refund)" \
  '**Verdict:** revise' '' "$RESULT_JSON_APPROVE" 1 "drafting" ""

echo ""
echo "--- E1 (t015 Codex P2): prose=approve + envelope is_error:true → 信頼しない ---"
_run_case "E1: approve + structured envelope is_error:true → drafting/null (refund)" \
  '**Verdict:** approve' '' "$RESULT_JSON_ERROR_APPROVE" 0 "drafting" ""

echo ""
echo "--- t018 FAIL-A (QA t016 B_k1〜B_k5 × structured=approve): ready にならず refund ---"
_run_case "B_k1: 1行目 revise + 本文に approve + structured=approve (1秒遅延) → drafting/null" \
  "$PROSE_K1" '' "$RESULT_JSON_APPROVE" 1 "drafting" ""
_run_case "B_k2: 1行目 approve + 本文に revise + structured=approve → drafting/null" \
  "$PROSE_K2" '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "B_k3: '**Verdict:** revise (重大な指摘あり)' + structured=approve → drafting/null" \
  "$PROSE_K3" '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "B_k4: '**Verdict:** REVISE' + structured=approve → drafting/null" \
  "$PROSE_K4" '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "B_k5: 1行目 reject + 本文に approve + structured=approve → drafting/null" \
  "$PROSE_K5" '' "$RESULT_JSON_APPROVE" 0 "drafting" ""

echo ""
echo "--- t018: approve 引用の記法を問わない (1行目 revise + 引用 approve + structured=approve) ---"
_run_case "Q_blockquote: 1行目 revise + '> **Verdict:** approve' → drafting/null" \
  $'**Verdict:** revise\n\n前回の判定:\n\n> **Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "Q_indent: 1行目 revise + '    **Verdict:** approve' → drafting/null" \
  $'**Verdict:** revise\n\n書式:\n\n    **Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "Q_list: 1行目 revise + '- **Verdict:** approve' → drafting/null" \
  $'**Verdict:** revise\n\n- **Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "Q_fence: 1行目 revise + フェンス内 approve → drafting/null" \
  $'**Verdict:** revise\n\n```\n**Verdict:** approve\n```' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""

echo ""
echo "--- t018: その他の書式違反 × structured=approve ---"
_run_case "DUP: 1行目 approve + 本文にも approve (同じ値の重複, Director 設計判断2) → drafting/null" \
  $'**Verdict:** approve\n\nnote\n\n**Verdict:** approve' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "NOBOLD: 'Verdict: revise' (太字なし, 兆候の上位集合) → drafting/null" \
  'Verdict: revise' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "BADUTF8: 兆候の無い別表記 + 不正な UTF-8 バイト (兆候の有無を確認できない) → drafting/null" \
  $'## 総合判定: GO\n\xff\xfe' '' "$RESULT_JSON_APPROVE" 0 "drafting" ""
_run_case "VIOL_NOSTRUCT: B_k1 + structured 無し → drafting/null" \
  "$PROSE_K1" '' '' 0 "drafting" ""

echo ""
echo "--- t018: 救済と正常系は残る ---"
_run_case "RESCUE: 兆候なしの別表記 (## 総合判定: GO) + structured=approve → ready/approve" \
  $'# Plan Review: testmission\n\n## 総合判定: GO' '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
_run_case "RESCUE_REVISE: 兆候なしの別表記 + structured=revise → drafting/revise" \
  $'# Plan Review: testmission\n\n## 総合判定: 要修正' '' "$RESULT_JSON_REVISE" 0 "drafting" "revise"
_run_case "NORMAL: 1行目 approve のみ + 雛形の注記 '(verdict が…)' + structured=approve (1秒遅延) → ready/approve" \
  $'**Verdict:** approve\n\n# Plan Review: testmission\n\n## Issues\n(verdict が revise/reject の場合のみ記載)' '' "$RESULT_JSON_APPROVE" 1 "ready" "approve"

if _selected "Decision1"; then
  echo ""
  echo "--- Decision1 直接確認: plan_review.md が review-plan.sh 終了後に外部から書き換わっても、plan.sh は plan_review.verdict の値だけを使う ---"
  # review-plan.sh 自体は正常終了 (revise) させ、plan.sh が plan_review.verdict を
  # 読む前に plan_review.md を外部から approve に書き換える。
  T_BIND="/tmp/crewvia-test-verdict-binding-direct-$$"
  _cleanup_dir "$T_BIND"
  mkdir -p "$T_BIND/scripts" "$T_BIND/config"
  export CREWVIA_QUEUE="$T_BIND/queue"
  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$T_BIND/scripts/"
  cp "$OWN_CHECKOUT_ROOT/scripts/lib_verdict.py" "$T_BIND/scripts/"
  cp -r "$OWN_CHECKOUT_ROOT/config/." "$T_BIND/config/"
  "$OWN_CHECKOUT_ROOT/scripts/plan.sh" init "Test Mission" --mission testmission >/dev/null 2>&1
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
printf '%s\nrun_id=%s\n' 'revise' "\${CREWVIA_PLAN_REVIEW_RUN_ID:-}" > "\${VERDICT_FILE}.tmp" && mv "\${VERDICT_FILE}.tmp" "\$VERDICT_FILE"
# review-plan.sh が確定して exit した後に、何者かが plan_review.md を書き換える最悪ケース。
printf '%s\n' '**Verdict:** approve' > "\$REVIEW_OUTPUT"
exit 0
EOF
  chmod +x "$T_BIND/scripts/review-plan.sh"
  BIND_RC=0
  BIND_OUT="$("$OWN_CHECKOUT_ROOT/scripts/plan.sh" review testmission 2>&1)" || BIND_RC=$?
  BIND_STATUS="$(awk '/^status:/ { print $2; exit }' "$T_BIND/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  BIND_VERDICT="$(awk '/^  last_verdict:/ { print $2; exit }' "$T_BIND/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  if [[ "$BIND_STATUS" == "drafting" && "$BIND_VERDICT" == "revise" ]]; then
    pass "Decision1: plan_review.md の事後書き換え (revise→approve) は無視され、plan_review.verdict の revise が消費される — status=$BIND_STATUS verdict=$BIND_VERDICT rc=$BIND_RC"
  else
    fail "Decision1: plan_review.md の事後書き換えが消費されてしまった (TOCTOU regression) — status=$BIND_STATUS verdict=$BIND_VERDICT (output: $BIND_OUT)"
  fi
  _cleanup_dir "$T_BIND"
  unset CREWVIA_QUEUE
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="

if [[ "$VERIFY_RED" -eq 1 ]]; then
  COUNTER_GROUP="red"
  echo ""
  echo "== --verify-red: 脆弱版を commit sha で固定して実行し、fail-open が再現することを確認する (本体スイートのカウンタは変えない) =="

  # $1 = short sha, $2 = 取り出し先。成功したら full sha を標準出力に出す。
  _prepare_pinned_root() {
    local sha="$1" dest="$2" full
    full="$(git -C "$GIT_REPO" rev-parse --verify "${sha}^{commit}" 2>/dev/null)" || return 1
    _cleanup_dir "$dest"
    mkdir -p "$dest"
    git -C "$GIT_REPO" archive "$full" scripts config | tar -x -C "$dest" || return 1
    printf '%s\n' "$full"
  }

  # --- 3388334 (t002) ---
  RED_ROOT_T002="/tmp/crewvia-test-verdict-binding-red-t002-$$"
  if FULL_T002="$(_prepare_pinned_root "$RED_SHA_T002" "$RED_ROOT_T002")"; then
    echo "  pinned $RED_SHA_T002 = $FULL_T002 (extracted with git archive into $RED_ROOT_T002)"
    SRC_ROOT="$RED_ROOT_T002"
    PINNED_SHA="$FULL_T002"
    _run_case "[RED $RED_SHA_T002] T1 (revise → kill 中に approve へ再Write) must reproduce ready/approve" \
      '**Verdict:** revise' '**Verdict:** approve' '' 0 "ready" "approve"
    _run_case "[RED $RED_SHA_T002] T1r (reject → kill 中に approve へ再Write) must reproduce ready/approve" \
      '**Verdict:** reject' '**Verdict:** approve' '' 0 "ready" "approve"
    _run_case "[RED $RED_SHA_T002] E1 (approve + envelope is_error:true) must reproduce ready/approve" \
      '**Verdict:** approve' '' "$RESULT_JSON_ERROR_APPROVE" 0 "ready" "approve"

    if _selected "[RED $RED_SHA_T002] CR"; then
      CR_FILE="$RED_ROOT_T002/cr_case.md"
      printf '**Verdict:** approve\rNOT approved; revisions required' > "$CR_FILE"
      CR_WANT="$(git -C "$GIT_REPO" rev-parse "$FULL_T002:scripts/lib_verdict.py" 2>/dev/null)"
      CR_GOT="$(git -C "$GIT_REPO" hash-object --no-filters "$RED_ROOT_T002/scripts/lib_verdict.py" 2>/dev/null)"
      CR_OLD_RC=0
      CR_OLD_OUT="$(python3 "$RED_ROOT_T002/scripts/lib_verdict.py" "$CR_FILE" 2>/dev/null)" || CR_OLD_RC=$?
      CR_NEW_RC=0
      CR_NEW_OUT="$(python3 "$OWN_CHECKOUT_ROOT/scripts/lib_verdict.py" "$CR_FILE" 2>/dev/null)" || CR_NEW_RC=$?
      echo "    executed scripts/lib_verdict.py blob=${CR_GOT:-?} | $FULL_T002:scripts/lib_verdict.py=${CR_WANT:-?}"
      echo "    tree under test: rc=$CR_NEW_RC out='${CR_NEW_OUT}'"
      if [[ -n "$CR_WANT" && "$CR_GOT" == "$CR_WANT" && "$CR_OLD_RC" -eq 0 && "$CR_OLD_OUT" == "approve" ]]; then
        pass "[RED $RED_SHA_T002] CR (FAIL-2): pinned lib_verdict.py CLI reads '**Verdict:** approve<CR>NOT approved…' as approve (rc=0) — red reproduced"
      else
        fail "[RED $RED_SHA_T002] CR (FAIL-2): expected pinned lib_verdict.py to return approve rc=0, got rc=$CR_OLD_RC out='$CR_OLD_OUT' (blob ok: $([[ "$CR_GOT" == "$CR_WANT" ]] && echo yes || echo no))"
      fi
    fi
  else
    fail "[RED $RED_SHA_T002] could not extract pinned revision $RED_SHA_T002 from $GIT_REPO (git archive failed or commit missing)"
  fi
  _cleanup_dir "$RED_ROOT_T002"

  # --- b21d4b4 (t015) ---
  RED_ROOT_T015="/tmp/crewvia-test-verdict-binding-red-t015-$$"
  if FULL_T015="$(_prepare_pinned_root "$RED_SHA_T015" "$RED_ROOT_T015")"; then
    echo "  pinned $RED_SHA_T015 = $FULL_T015 (extracted with git archive into $RED_ROOT_T015)"
    SRC_ROOT="$RED_ROOT_T015"
    PINNED_SHA="$FULL_T015"
    _run_case "[RED $RED_SHA_T015] B_k1 (revise + 本文 approve + structured=approve) must reproduce ready/approve" \
      "$PROSE_K1" '' "$RESULT_JSON_APPROVE" 1 "ready" "approve"
    _run_case "[RED $RED_SHA_T015] B_k2 (approve + 本文 revise + structured=approve) must reproduce ready/approve" \
      "$PROSE_K2" '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
    _run_case "[RED $RED_SHA_T015] B_k3 (revise (重大な指摘あり) + structured=approve) must reproduce ready/approve" \
      "$PROSE_K3" '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
    _run_case "[RED $RED_SHA_T015] B_k4 (REVISE + structured=approve) must reproduce ready/approve" \
      "$PROSE_K4" '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
    _run_case "[RED $RED_SHA_T015] B_k5 (reject + 本文 approve + structured=approve) must reproduce ready/approve" \
      "$PROSE_K5" '' "$RESULT_JSON_APPROVE" 0 "ready" "approve"
  else
    fail "[RED $RED_SHA_T015] could not extract pinned revision $RED_SHA_T015 from $GIT_REPO (git archive failed or commit missing)"
  fi
  _cleanup_dir "$RED_ROOT_T015"
  SRC_ROOT="$OWN_CHECKOUT_ROOT"
  PINNED_SHA=""

  # --- G2 メタテスト (QA t016 Finn G2) ---
  if [[ "${BINDING_E2E_SKIP_META:-0}" != "1" ]] && _selected "[META] G2"; then
    echo ""
    echo "--- [META] G2: 作業ツリーに脆弱版 ($RED_SHA_T002 の review-plan.sh / plan.sh) を置いて --verify-red で実行 → 本体の失敗が非 0 終了に反映される ---"
    META="/tmp/crewvia-test-verdict-binding-meta-$$"
    _cleanup_dir "$META"
    mkdir -p "$META"
    cp -r "$OWN_CHECKOUT_ROOT/scripts" "$META/scripts"
    cp -r "$OWN_CHECKOUT_ROOT/config" "$META/config"
    if git -C "$GIT_REPO" show "${RED_SHA_T002}:scripts/review-plan.sh" > "$META/scripts/review-plan.sh" \
        && git -C "$GIT_REPO" show "${RED_SHA_T002}:scripts/plan.sh" > "$META/scripts/plan.sh"; then
      chmod +x "$META/scripts/review-plan.sh" "$META/scripts/plan.sh"
      META_RC=0
      META_OUT="$(BINDING_E2E_GIT_REPO="$GIT_REPO" BINDING_E2E_SKIP_META=1 \
        BINDING_E2E_CASE_FILTER='(^|\] )T1 \(' \
        bash "$META/scripts/test_review_plan_verdict_binding_e2e.sh" --verify-red 2>&1)" || META_RC=$?
      printf '%s\n' "$META_OUT" | grep -E '^(  PASS|  FAIL|== )' | sed 's/^/    [meta] /' | cut -c1-240
      if [[ "$META_RC" -ne 0 ]] \
          && printf '%s\n' "$META_OUT" | grep -q '^  FAIL: T1 (' \
          && printf '%s\n' "$META_OUT" | grep -q "^  PASS: \[RED $RED_SHA_T002\] T1 ("; then
        pass "[META] G2: 本体 T1 が FAIL し red 確認が PASS した状態でも exit=$META_RC (非 0) — 本体の失敗は握り潰されない"
      else
        fail "[META] G2: 期待 = 本体 T1 FAIL かつ red T1 PASS かつ非 0 終了。実際 exit=$META_RC"
      fi
    else
      fail "[META] G2: could not extract $RED_SHA_T002 scripts for the meta test"
    fi
    _cleanup_dir "$META"
  fi

  COUNTER_GROUP="main"
  echo ""
  echo "== RED results (pinned vulnerable revisions must reproduce the fail-open): $RED_PASS_COUNT reproduced/confirmed, $RED_FAIL_COUNT NOT reproduced =="
fi

echo ""
echo "== Final: main $PASS_COUNT passed / $FAIL_COUNT failed; red failures $RED_FAIL_COUNT =="
[[ "$FAIL_COUNT" -eq 0 && "$RED_FAIL_COUNT" -eq 0 ]]
