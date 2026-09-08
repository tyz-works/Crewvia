#!/usr/bin/env bats
# tests/plan-wrapper.bats
#
# scripts/bin/plan — PATH ラッパーの回帰テスト。
# mission 20260908-main-repo-protection / t002:
#   Worker が plan.sh を「呼び出す」ために $CREWVIA_REPO_ROOT の絶対パスを
#   打つ習慣を無くす目的で追加された薄いラッパー。引数・終了コード・
#   stdout/stderr が素の scripts/plan.sh と完全に一致することが必須要件。
#
# Run:
#   bats tests/plan-wrapper.bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
PLAN_WRAPPER="${REPO_ROOT}/scripts/bin/plan"
PLAN_SH="${REPO_ROOT}/scripts/plan.sh"

@test "scripts/bin/plan は存在し実行可能" {
  [ -f "$PLAN_WRAPPER" ]
  [ -x "$PLAN_WRAPPER" ]
}

@test "plan status の stdout / 終了コードが plan.sh status と完全一致する (成功系)" {
  wrapper_out=$(CREWVIA_REPO_ROOT="$REPO_ROOT" "$PLAN_WRAPPER" status 2>&1)
  wrapper_rc=$?
  direct_out=$("$PLAN_SH" status 2>&1)
  direct_rc=$?

  [ "$wrapper_out" = "$direct_out" ]
  [ "$wrapper_rc" -eq "$direct_rc" ]
}

@test "plan <不正サブコマンド> の stdout/stderr/終了コードが plan.sh と完全一致する (失敗系)" {
  wrapper_out=$(CREWVIA_REPO_ROOT="$REPO_ROOT" "$PLAN_WRAPPER" bogus-subcommand 2>/tmp/plan_wrapper_test_err.$$) || wrapper_rc=$?
  wrapper_rc="${wrapper_rc:-0}"
  wrapper_err=$(cat /tmp/plan_wrapper_test_err.$$); rm -f /tmp/plan_wrapper_test_err.$$

  direct_out=$("$PLAN_SH" bogus-subcommand 2>/tmp/plan_direct_test_err.$$) || direct_rc=$?
  direct_rc="${direct_rc:-0}"
  direct_err=$(cat /tmp/plan_direct_test_err.$$); rm -f /tmp/plan_direct_test_err.$$

  [ "$wrapper_out" = "$direct_out" ]
  [ "$wrapper_err" = "$direct_err" ]
  [ "$wrapper_rc" -eq "$direct_rc" ]
}

@test "CREWVIA_REPO_ROOT / CREWVIA_REPO が両方とも未設定なら明確なエラーで exit 1" {
  run env -u CREWVIA_REPO_ROOT -u CREWVIA_REPO "$PLAN_WRAPPER" status
  [ "$status" -eq 1 ]
  [[ "$output" == *"CREWVIA_REPO_ROOT"* ]]
}

@test "CREWVIA_REPO_ROOT 未設定でも CREWVIA_REPO があれば解決できる (フォールバック)" {
  run env -u CREWVIA_REPO_ROOT CREWVIA_REPO="$REPO_ROOT" "$PLAN_WRAPPER" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"Active missions"* || "$output" == *"No active missions"* || "$output" == *"Mission:"* ]]
}

@test "既存の絶対パス呼び出し (\$CREWVIA_REPO_ROOT/scripts/plan.sh) は後方互換で引き続き動く" {
  run env CREWVIA_REPO_ROOT="$REPO_ROOT" bash -c '"$CREWVIA_REPO_ROOT/scripts/plan.sh" status'
  [ "$status" -eq 0 ]
}

@test "start.sh は scripts/bin を PATH の先頭に追加する (静的チェック)" {
  grep -q 'export PATH="\${REPO_ROOT}/scripts/bin:\${PATH}"' "${REPO_ROOT}/scripts/start.sh"
}

@test "start.sh の mux LAUNCH_CMD にも scripts/bin の PATH 追加が含まれる (静的チェック、herdr/tmux ペインは呼び出し元の env を継承しないため)" {
  grep -q "export PATH='\${REPO_ROOT}/scripts/bin:'" "${REPO_ROOT}/scripts/start.sh"
}

@test "Worker 向け KICKOFF_MSG は \$CREWVIA_REPO_ROOT の絶対パスを含まない (誘因除去の確認)" {
  ! grep -E 'KICKOFF_MSG=.*CREWVIA_REPO_ROOT.*scripts/plan\.sh' "${REPO_ROOT}/scripts/start.sh"
}

@test "dispatcher.sh の assign 通知メッセージも plan (wrapper) 形式を使う" {
  grep -q 'plan pull --task {task_id} --mission {slug}' "${REPO_ROOT}/scripts/dispatcher.sh"
  ! grep -q 'plan\.sh pull --task {task_id} --mission {slug}' "${REPO_ROOT}/scripts/dispatcher.sh"
}
