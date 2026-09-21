#!/usr/bin/env bash
# scripts/test_retirement_authority.sh
#
# t002 (mission 20260921-daemon-authority-and-mutual-watch) の受け入れテスト。
#
# 「Worker を強制終了する権限を watchdog に一元化し、後始末と中断耐性を実装する」
# を、実プロセス (本物の dispatcher.sh / watchdog.py) を隔離環境で動かして検証する。
# モックの再実装ではなく本番コードそのものを動かすのが要点
# (memory: crewvia-fake-cli-and-qa-fail-gaps — fake CLI のテストは応答形式のバグを隠す)。
#
# ## 隔離のしかた (本番を絶対に巻き込まない)
#
#   - repo        : $TMPDIR 以下に scripts/ + config/ をコピーした使い捨ての git checkout。
#                   本番 repo の queue/ registry/ には一切触れない。
#   - mux         : CREWVIA_MUX=tmux + CREWVIA_TMUX_SESSION=crewvia-retiretest-$$。
#                   本番は herdr backend なので tmux backend からは原理的に見えない。
#                   さらに専用セッション名なのでユーザーの tmux セッションとも別。
#   - Worker 役   : `sleep 9999` を走らせた tmux 窓。Claude ではないので
#                   殺されても失うものが無い。
#
# ## テストケース
#
#   case1 ghost-task       : watchdog が Worker を terminate した後、task が pending に
#                            戻り assignment が消えること (E1: 後始末の穴)
#   case2 interrupt-resume : terminate の途中で watchdog を kill -9 しても、
#                            (a) 中断時点の進捗がディスクに残り
#                            (b) 再起動した watchdog が続きを引き取って完結させること (R1-R4)
#   case3 dispatcher-no-kill: dispatcher は用済み Worker の窓を自分で kill せず、
#                            retirement marker を書くだけになること (§3-4 D1)
#   case4 identity-guard   : marker の spawn_identity が現在の窓と一致しない場合、
#                            watchdog は殺さず discarded にすること (R7 / §3-3 F2)
#
# 実行:
#   bash scripts/test_retirement_authority.sh
#   bash scripts/test_retirement_authority.sh case1     # 単体
#
# 注意: set -e は使わない。テストスクリプトで set -e が漏れると FAIL 行も Results も
# 出ないまま rc=1 で黙って中断する (memory: test-silent-abort-leaked-set-e)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MISSION="20260921-retiretest"
TMUX_SESSION="crewvia-retiretest-$$"
SANDBOX_BASE="${TMPDIR:-/tmp}/crewvia-retire-$$"

PASS=0
FAIL=0
FAILED_CASES=()

pass() { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }
info() { echo "  ---- $*"; }

# ---------------------------------------------------------------------------
# Sandbox construction
# ---------------------------------------------------------------------------

# make_sandbox <name> — echo the sandbox path on stdout.
make_sandbox() {
  local sb="${SANDBOX_BASE}/$1"
  mkdir -p "$sb"
  cp -a "${REPO_ROOT}/scripts" "$sb/scripts"
  mkdir -p "$sb/config"
  # 本番 config をコピーせず最小構成を書く。mode: tmux を明示して
  # herdr backend を絶対に選ばせない (env CREWVIA_MUX=tmux との二重化)。
  cat > "$sb/config/crewvia.yaml" <<'CFG'
mode: tmux
mux:
  state_grace_seconds: 60
CFG
  ( cd "$sb" && git init -q && git config user.email t@example.invalid && git config user.name tester ) >/dev/null 2>&1
  mkdir -p "$sb/queue/missions/${MISSION}/tasks" "$sb/queue/assignments" \
           "$sb/registry/mux" "$sb/registry/heartbeats" "$sb/logs"
  cat > "$sb/registry/workers.yaml" <<'WRK'
workers:
  TestAgent:
    role: worker
    skills:
      - bash
WRK
  echo "$sb"
}

# write_state <sandbox> <active_missions_yaml_body>
write_state() {
  local sb="$1"; shift
  printf 'active_missions:\n%s\ndefault_mission: %s\n' "$1" "$MISSION" > "$sb/queue/state.yaml"
  cat > "$sb/queue/missions/${MISSION}/mission.yaml" <<MIS
title: retirement authority test
status: in_progress
next_task_id: 2
MIS
}

# write_task <sandbox> <status> <worker|null> <max_seconds>
write_task() {
  local sb="$1" status="$2" worker="$3" maxs="$4"
  cat > "$sb/queue/missions/${MISSION}/tasks/t001.md" <<TASK
---
id: t001
title: retirement test task
status: ${status}
worker: ${worker}
skills: [bash]
priority: high
blocked_by: []
timeout:
  idle: 1
  max: ${maxs}
---

## Description

placeholder

## Result

TASK
}

# spawn_fake_worker <name> — create a tmux window running `sleep 9999`.
spawn_fake_worker() {
  local name="$1"
  tmux -f /dev/null new-session -d -s "$TMUX_SESSION" -n "$name" "sleep 9999" 2>/dev/null \
    || tmux new-window -t "$TMUX_SESSION" -n "$name" "sleep 9999"
}

window_alive() {
  tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$1"
}

task_status() {
  sed -n 's/^status: //p' "$1/queue/missions/${MISSION}/tasks/t001.md" | head -1
}

# wait_until <timeout_seconds> <shell-condition...> — return 0 as soon as it holds.
wait_until() {
  local deadline=$(( SECONDS + $1 )); shift
  while (( SECONDS < deadline )); do
    if eval "$@"; then return 0; fi
    sleep 1
  done
  return 1
}

cleanup() {
  # 自分のセッションだけを落とす。他のセッションには触れない。
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null
  if [[ -n "${KEEP_SANDBOX:-}" ]]; then
    echo "sandbox kept: ${SANDBOX_BASE}"
  else
    # rm -rf は global deny (memory: qa-isolated-copy-without-rm-rf)。
    # /tmp 配下の使い捨てなので退避だけして残す。
    [[ -d "$SANDBOX_BASE" ]] && mv "$SANDBOX_BASE" "${SANDBOX_BASE}.done" 2>/dev/null
  fi
}
trap cleanup EXIT

# common env for daemons under test
daemon_env() {
  local sb="$1"
  # CREWVIA_NOTIFY_CACHE: 本番の /tmp/dispatcher-notify-cache.json を共有すると、
  # 前回実行の残留キーで marker 書き込みが dedup 抑止され偽 FAIL になる
  # (memory: dispatcher-isolated-qa-harness)。sandbox 固有のパスに逃がす。
  echo "CREWVIA_MUX=tmux CREWVIA_TMUX_SESSION=${TMUX_SESSION} CREWVIA_QUEUE=${sb}/queue CREWVIA_TASKVIA=disabled TASKVIA_TOKEN= CREWVIA_NOTIFY_CACHE=${sb}/notify-cache.json"
}

# ---------------------------------------------------------------------------
# case1: watchdog terminate must leave no ghost task
# ---------------------------------------------------------------------------
case1() {
  echo "[case1] ghost-task — watchdog terminate の後始末"
  local sb; sb="$(make_sandbox case1)"
  write_state "$sb" "  - ${MISSION}"
  write_task "$sb" in_progress TestAgent 1
  echo "${MISSION}:t001" > "$sb/queue/assignments/TestAgent"
  spawn_fake_worker "TestAgent-worker"

  env $(daemon_env "$sb") \
    python3 "$sb/scripts/watchdog.py" --repo-root "$sb" --interval 3 \
    > "$sb/logs/watchdog.out" 2>&1 &
  local wd=$!

  if wait_until 150 '! window_alive TestAgent-worker'; then
    pass "Worker 窓が terminate された"
  else
    fail "Worker 窓が 150s 以内に terminate されなかった"
  fi
  # 後始末が非同期なので数サイクル待つ
  wait_until 60 '[[ "$(task_status '"$sb"')" == pending ]]'
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null

  local st; st="$(task_status "$sb")"
  if [[ "$st" == "pending" ]]; then
    pass "task が pending に戻っている"
  else
    fail "task status=${st} (期待: pending) — 幽霊 task が残っている (E1)"
  fi
  if [[ ! -e "$sb/queue/assignments/TestAgent" ]]; then
    pass "assignment が削除されている"
  else
    fail "assignment/TestAgent が残っている (E1)"
  fi

  # task は後始末が終わるまで in_progress のままなので、monitor を毎 cycle
  # 作り直すと同じ終了に対して TERMINATE 宣言と Taskvia alert が cycle ごとに
  # 出る (この assert を入れる前の実測で 1 回の終了に 12 回)。
  local n_term
  n_term="$(grep -c 'TERMINATE:' "$sb/logs/watchdog.out" 2>/dev/null || echo 0)"
  if (( n_term <= 2 )); then
    pass "TERMINATE の宣言が ${n_term} 回に収まっている (重複通知なし)"
  else
    fail "TERMINATE が ${n_term} 回宣言された — 終了処理中の Worker を再監視している"
  fi
  info "watchdog log: $sb/logs/watchdog.out"
}

# ---------------------------------------------------------------------------
# case2: interruption resilience
# ---------------------------------------------------------------------------
case2() {
  echo "[case2] interrupt-resume — terminate 中に watchdog が落ちても完結する"
  local sb; sb="$(make_sandbox case2)"
  write_state "$sb" "  - ${MISSION}"
  write_task "$sb" in_progress TestAgent 1
  echo "${MISSION}:t001" > "$sb/queue/assignments/TestAgent"
  spawn_fake_worker "TestAgent-worker"

  env $(daemon_env "$sb") \
    python3 "$sb/scripts/watchdog.py" --repo-root "$sb" --interval 3 \
    > "$sb/logs/watchdog1.out" 2>&1 &
  local wd1=$!

  # terminate 判定に入るまで待ってから、破壊ステップの途中で強制終了する。
  if wait_until 60 'grep -q TERMINATE: "'"$sb"'/logs/watchdog1.out"'; then
    info "terminate 開始を検知 — 3s 後に watchdog を kill -9 する"
  else
    fail "watchdog が terminate 判定に入らなかった"
  fi
  sleep 3
  kill -9 "$wd1" 2>/dev/null; wait "$wd1" 2>/dev/null

  if window_alive TestAgent-worker; then
    info "中断時点で Worker はまだ生きている (想定どおり)"
  fi

  # (a) 中断時点の進捗がディスクに残っているか
  if compgen -G "$sb/registry/retirements/*.progress.json" > /dev/null; then
    pass "中断時点の進捗がディスクに永続化されている"
    info "$(cat "$sb"/registry/retirements/*.progress.json)"
  else
    fail "registry/retirements/*.progress.json が無い — 中断で終了処理が宙に浮く (R1)"
  fi

  # (b) 再起動した watchdog が続きを引き取るか
  env $(daemon_env "$sb") \
    python3 "$sb/scripts/watchdog.py" --repo-root "$sb" --interval 3 \
    > "$sb/logs/watchdog2.out" 2>&1 &
  local wd2=$!
  if wait_until 150 '! window_alive TestAgent-worker'; then
    pass "再起動後の watchdog が Worker を終了させた"
  else
    fail "再起動後も Worker が生き残った (R3 の回収が無い)"
  fi
  wait_until 60 '[[ "$(task_status '"$sb"')" == pending ]]'
  kill "$wd2" 2>/dev/null; wait "$wd2" 2>/dev/null

  local st; st="$(task_status "$sb")"
  if [[ "$st" == "pending" ]]; then
    pass "再起動後に task が pending へ戻った"
  else
    fail "task status=${st} (期待: pending) — 中断後の後始末が完結していない"
  fi
}

# ---------------------------------------------------------------------------
# case3: dispatcher must hand over instead of killing
# ---------------------------------------------------------------------------
case3() {
  echo "[case3] dispatcher-no-kill — dispatcher は marker を書くだけ"
  local sb; sb="$(make_sandbox case3)"
  # active_missions 空 → shutdown_idle_workers() (D1) が走る
  printf 'active_missions: []\ndefault_mission: %s\n' "$MISSION" > "$sb/queue/state.yaml"
  spawn_fake_worker "TestAgent-worker"
  # spawn grace (90s) を明示的に満了させる
  echo "1000000000" > "$sb/registry/mux/TestAgent-worker.firstseen"

  env $(daemon_env "$sb") \
    bash "$sb/scripts/dispatcher.sh" > "$sb/logs/dispatcher.out" 2>&1 &
  local dp=$!
  sleep 14
  kill "$dp" 2>/dev/null; pkill -f "$sb/scripts/dispatcher.sh" 2>/dev/null; wait "$dp" 2>/dev/null

  if window_alive TestAgent-worker; then
    pass "dispatcher は窓を kill しなかった"
  else
    fail "dispatcher が窓を kill した — kill 権限が移譲されていない"
  fi
  if [[ -f "$sb/registry/retirements/TestAgent.json" ]]; then
    pass "retirement marker が書かれた"
    info "$(cat "$sb/registry/retirements/TestAgent.json")"
  else
    fail "registry/retirements/TestAgent.json が無い — 引き渡しが行われていない"
  fi
  info "dispatcher log: $sb/logs/dispatcher.out"
}

# ---------------------------------------------------------------------------
# case4: R7 — stale marker must not kill a same-named new instance
# ---------------------------------------------------------------------------
case4() {
  echo "[case4] identity-guard — spawn_identity 不一致なら殺さない (R7)"
  local sb; sb="$(make_sandbox case4)"
  printf 'active_missions: []\ndefault_mission: %s\n' "$MISSION" > "$sb/queue/state.yaml"
  spawn_fake_worker "TestAgent-worker"

  mkdir -p "$sb/registry/retirements"
  # 別インスタンスを指す marker: pane_pid も created_at も現在の窓と一致しない
  cat > "$sb/registry/retirements/TestAgent.json" <<'MK'
{"agent": "TestAgent", "window_target": "TestAgent-worker", "reason": "no-task",
 "message": "タスクなし、shutdown", "requested_at": 1000000000,
 "mission": null, "task_id": null,
 "spawn_identity": {"pane_pid": 999999, "created_at": 1000000000.0}}
MK

  env $(daemon_env "$sb") \
    python3 "$sb/scripts/watchdog.py" --repo-root "$sb" --interval 3 \
    > "$sb/logs/watchdog.out" 2>&1 &
  local wd=$!
  sleep 15
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null

  if window_alive TestAgent-worker; then
    pass "identity 不一致の窓は殺されなかった"
  else
    fail "identity 不一致にもかかわらず窓が殺された (R7 違反 — 同名の別 Worker 殺し)"
  fi
  # discarded は終端なので marker は次の cycle で消える。残骸を期待すると
  # 「消える前に見に行けたか」というレースを検証することになるので、
  # 判定そのものはログで、後始末は marker の不在で確かめる。
  if grep -q 'NOT retiring' "$sb/logs/watchdog.out"; then
    pass "identity 不一致として判定された"
    info "$(grep -m1 'NOT retiring' "$sb/logs/watchdog.out")"
  else
    fail "identity 不一致の判定がログに出ていない"
    info "watchdog log tail: $(tail -5 "$sb/logs/watchdog.out")"
  fi
  if [[ ! -f "$sb/registry/retirements/TestAgent.json" \
     && ! -f "$sb/registry/retirements/TestAgent.progress.json" ]]; then
    pass "不一致 marker が片付けられた"
  else
    fail "不一致 marker が残っている — 次 cycle 以降も後継 Worker を狙い続ける"
  fi
}

# ---------------------------------------------------------------------------

main() {
  local cases=("$@")
  if [[ ${#cases[@]} -eq 0 ]]; then
    cases=(case1 case2 case3 case4)
  fi
  echo "=== retirement authority test (session=${TMUX_SESSION}, sandbox=${SANDBOX_BASE}) ==="
  for c in "${cases[@]}"; do
    local before=$FAIL
    "$c"
    if (( FAIL > before )); then FAILED_CASES+=("$c"); fi
    echo
  done
  echo "=== Results: PASS=${PASS} FAIL=${FAIL} ==="
  if (( FAIL > 0 )); then
    echo "failed cases: ${FAILED_CASES[*]}"
    return 1
  fi
  return 0
}

main "$@"
