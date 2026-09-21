#!/usr/bin/env bash
# tests/watchdog-idle-e2e.sh
#
# t016: watchdog の warn / terminate が **実際に発火する** ことを、しきい値を
# 小さくした隔離環境で実証する。
#
# ## なぜ unit テストだけでは足りないか
#
# tests/test_watchdog_idle.py は check() の判定を直接叩く。だが今回直した欠陥は
# 「判定は正しく書かれているのに、その手前の早期 return で到達しない」という
# 形だった。同種の欠陥は、常駐ループ・ログ経路・graceful_terminate() まで通しで
# 動かさない限り「テストは緑なのに本番では一度も発火しない」で再発しうる
# (memory: crewvia-recurring-defect-patterns「本番で発火しない dead code」)。
#
# ここでは本物の watchdog.py を **本物のデーモンとして** 起動し、本物のプロセスを
# 相手に、ログと生死で結果を確かめる。
#
# ## 隔離の方法 (本番に一切触れない)
#
#   - repo_root は mktemp -d の一時ディレクトリ (--repo-root で渡す)
#   - scripts/ に watchdog.py を **コピー** し、隣に **偽 lib_mux.py** を置く。
#     watchdog.py は自分の隣から lib_mux を import するので、本番の herdr / tmux
#     には一切接続しない (memory: dispatcher-isolated-qa-harness)
#   - 監視対象は自分で spawn した sleep の木。本番 Worker は巻き込まない
#   - TASKVIA_TOKEN を空にして外部 POST を無効化する
#
# 小さくするしきい値:
#   - task frontmatter の timeout.idle          … 正規の入力なのでそのまま使う
#   - TERMINATE_GRACE_PERIOD / KILL_DELAY       … 60s + 10s 待てないのでコピーを書換
#   - PROCESS_WORK_START_GRACE                  … 60s 待てないのでコピーを書換
#
# 実行:
#   bash tests/watchdog-idle-e2e.sh

# set -e は使わない。途中で落ちると FAIL も Results も出ないまま rc=1 で終わり、
# 原因の切り分けが効かなくなる (memory: test-silent-abort-leaked-set-e)。
set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

ok()   { echo "  PASS: $*"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }
info() { echo "  ---- $*"; }

# ---------------------------------------------------------------------------
# 隔離環境の構築
# ---------------------------------------------------------------------------

ROOT="$(mktemp -d -t crewvia-watchdog-e2e-XXXXXX)"
echo "隔離 repo_root: $ROOT"

mkdir -p "$ROOT/scripts" "$ROOT/registry" "$ROOT/queue/missions/e2e/tasks"

# repo_identity_ok() が本物と同じ意味を持つよう、実際に git checkout にする
git init -q "$ROOT" 2>/dev/null || true

cp "$SRC_DIR/scripts/watchdog.py" "$ROOT/scripts/watchdog.py"

# 待ち時間としきい値を縮める (判定ロジックそのものは書き換えない)
sed -i \
  -e 's/^TERMINATE_GRACE_PERIOD = .*/TERMINATE_GRACE_PERIOD = 2/' \
  -e 's/^KILL_DELAY = .*/KILL_DELAY = 1/' \
  -e 's/^PROCESS_WORK_START_GRACE = .*/PROCESS_WORK_START_GRACE = 1/' \
  "$ROOT/scripts/watchdog.py"

# 偽 lib_mux — 本番 backend には触れない
cat_fake_mux() {
  cat > "$ROOT/scripts/lib_mux.py" <<'PYEOF'
"""E2E 用の偽 lib_mux。実 herdr / tmux には一切接続しない。

pane_pid は環境変数 E2E_PANE_PID から読む。窓名は固定。
"""
import os
from pathlib import Path

WINDOW = "E2EWorker-worker"


def repo_identity_ok(repo_root) -> bool:
    # 本物と同じ意味 — 自分の repo_root が git checkout として実在するか
    return Path(repo_root).exists() and (Path(repo_root) / ".git").exists()


class _FakeBackend:
    pass


class Mux:
    def __init__(self, backend=None):
        self._backend = _FakeBackend()

    def available(self) -> bool:
        return True

    def server_running(self) -> bool:
        return True

    def list(self, suffix=None):
        # pane が死んだら窓も消えたことにする (本番の挙動に合わせる)
        pid = os.environ.get("E2E_PANE_PID")
        if pid and Path(f"/proc/{pid}").exists():
            return [WINDOW]
        return []

    def pid(self, name):
        pid = os.environ.get("E2E_PANE_PID")
        return int(pid) if pid else None

    def send(self, name, text) -> bool:
        with open(os.environ["E2E_SEND_LOG"], "a") as f:
            f.write(f"{name}\t{text}\n")
        return True

    def kill(self, name) -> bool:
        return True
PYEOF
}
cat_fake_mux

cat > "$ROOT/queue/state.yaml" <<'YEOF'
active_missions:
  - e2e
YEOF

export E2E_SEND_LOG="$ROOT/send.log"
export TASKVIA_TOKEN=""
export CREWVIA_QUEUE="$ROOT/queue"

# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------

# $1 = idle 秒, $2 = activity を何秒前にするか
setup_task() {
  local idle="$1" activity_age="$2"
  cat > "$ROOT/queue/missions/e2e/tasks/t001.md" <<TEOF
---
id: t001
status: in_progress
worker: E2EWorker
timeout:
  idle: ${idle}
  max: 99999
---

## Description
e2e
TEOF
  mkdir -p "$ROOT/registry/activity/E2EWorker"
  local f="$ROOT/registry/activity/E2EWorker/t001.activity"
  echo "tool-use" > "$f"
  touch -d "@$(( $(date +%s) - activity_age ))" "$f" 2>/dev/null \
    || touch -d "$(date -d "-${activity_age} seconds" '+%Y-%m-%d %H:%M:%S')" "$f"
  # heartbeat も同じだけ古くしておく (idle は両者の新しい方で決まる)
  mkdir -p "$ROOT/registry/heartbeats"
  local h="$ROOT/registry/heartbeats/E2EWorker"
  echo "alive" > "$h"
  touch -r "$f" "$h"
}

today_log() { echo "$ROOT/logs/watchdog/watchdog-$(date +%Y%m%d).log"; }

# watchdog を隔離環境で n 秒だけ走らせる
run_watchdog() {
  local seconds="$1"
  python3 "$ROOT/scripts/watchdog.py" --repo-root "$ROOT" --interval 1 \
    >"$ROOT/watchdog.stderr" 2>&1 &
  local wd=$!
  sleep "$seconds"
  kill "$wd" 2>/dev/null
  wait "$wd" 2>/dev/null
}

# 監視対象の「ペイン」を立てる。
#   idle 木      : root -> sh -> sleep, sleep     (全部ほぼ同時 = MCP 相当だけ)
#   executing 木 : 上に加えて 2 秒後に生える子孫  (= Bash tool 実行中)
spawn_pane() {
  local kind="$1"
  local pidfile="$ROOT/pane.pid"
  rm -f "$pidfile"
  # 自分の PID をファイルに書かせる。setsid が fork するかどうかに依存せず
  # 「ペインのシェル」の PID を確実に取るため ($! では setsid 自身を掴みうる)。
  # setsid でプロセスグループを分けておくと、後片付けでグループごと殺せる。
  if [[ "$kind" == "executing" ]]; then
    setsid sh -c 'echo $$ > "$1"; sleep 300 & sh -c "sleep 2; sleep 300 & wait" & wait' _ "$pidfile" &
  else
    setsid sh -c 'echo $$ > "$1"; sh -c "sleep 300 & sleep 300 & wait" & wait' _ "$pidfile" &
  fi
  disown 2>/dev/null || true   # 後片付けの kill で "Killed" を出力させない
  local waited=0
  while [[ ! -s "$pidfile" && "$waited" -lt 50 ]]; do sleep 0.1; waited=$((waited + 1)); done
  PANE_PID="$(cat "$pidfile" 2>/dev/null)"
  export E2E_PANE_PID="$PANE_PID"
  sleep 0.5
}

cleanup_pane() {
  [[ -n "${PANE_PID:-}" ]] && kill -9 -- "-${PANE_PID}" 2>/dev/null
  [[ -n "${PANE_PID:-}" ]] && kill -9 "${PANE_PID}" 2>/dev/null
  PANE_PID=""
  return 0
}

pane_alive() { [[ -d "/proc/${PANE_PID}" ]]; }

# ---------------------------------------------------------------------------
# シナリオ 1: soft idle → warn が発火し、Worker は殺されない
# ---------------------------------------------------------------------------

echo
echo "[1] soft idle → warn (idle=30, 無音 40s)"
rm -f "$(today_log)" "$E2E_SEND_LOG" 2>/dev/null
setup_task 30 40
spawn_pane idle
run_watchdog 4

LOG="$(today_log)"
if grep -q 'reason=soft_idle' "$LOG" 2>/dev/null; then
  ok "warn が発火し判定がログに残った"
  info "$(grep -m1 'reason=soft_idle' "$LOG")"
else
  bad "soft_idle の判定行が無い"
  info "log: $(tail -3 "$LOG" 2>/dev/null)"
fi
grep -q 'WARN: E2EWorker/t001' "$LOG" 2>/dev/null \
  && ok "WARN アラート行が出た" || bad "WARN アラート行が無い"
pane_alive && ok "warn では Worker を殺さない" || bad "warn なのに Worker が死んだ"
cleanup_pane

# ---------------------------------------------------------------------------
# シナリオ 2: hard idle → terminate が発火し、実際にプロセスが死ぬ
# ---------------------------------------------------------------------------

echo
echo "[2] hard idle → terminate (idle=3, 無音 60s)"
rm -f "$(today_log)" "$E2E_SEND_LOG" 2>/dev/null
setup_task 3 60
spawn_pane idle
run_watchdog 10

LOG="$(today_log)"
if grep -q 'reason=hard_idle' "$LOG" 2>/dev/null; then
  ok "terminate が発火し判定がログに残った"
  info "$(grep -m1 'reason=hard_idle' "$LOG")"
else
  bad "hard_idle の判定行が無い"
  info "log: $(tail -5 "$LOG" 2>/dev/null)"
fi
grep -q 'TERMINATE: E2EWorker/t001' "$LOG" 2>/dev/null \
  && ok "TERMINATE 行が出た" || bad "TERMINATE 行が無い"
grep -q 'タイムアウトのため中断します' "$E2E_SEND_LOG" 2>/dev/null \
  && ok "graceful shutdown メッセージが送られた" || bad "shutdown メッセージが無い"
if pane_alive; then
  bad "terminate なのに Worker プロセスが生きている"
else
  ok "SIGTERM → SIGKILL まで通り、Worker プロセスが実際に消えた"
fi
cleanup_pane

# ---------------------------------------------------------------------------
# シナリオ 3: hard idle でも「実行中」なら殺さない (誤 terminate 防止)
# ---------------------------------------------------------------------------

echo
echo "[3] hard idle + Bash tool 実行中 → warn 止まり (idle=3, 無音 60s)"
rm -f "$(today_log)" "$E2E_SEND_LOG" 2>/dev/null
setup_task 3 60
spawn_pane executing
sleep 3   # 遅れて生える子孫が出そろうまで
run_watchdog 6

LOG="$(today_log)"
if grep -q 'reason=hard_idle_but_executing' "$LOG" 2>/dev/null; then
  ok "実行中を検出し terminate を warn に落とした"
  info "$(grep -m1 'reason=hard_idle_but_executing' "$LOG")"
else
  bad "hard_idle_but_executing の判定行が無い"
  info "log: $(tail -5 "$LOG" 2>/dev/null)"
fi
grep -q 'TERMINATE: E2EWorker/t001' "$LOG" 2>/dev/null \
  && bad "実行中なのに TERMINATE が出た" || ok "TERMINATE は出ていない"
pane_alive && ok "実行中の Worker は生き残った" || bad "実行中の Worker が殺された"
cleanup_pane

# ---------------------------------------------------------------------------
# シナリオ 4: 対照 — origin/main の実物は同じ条件で一切発火しない
# ---------------------------------------------------------------------------

echo
echo "[4] 対照: origin/main の watchdog.py を同条件で走らせる"
OLDROOT="$ROOT/old"
mkdir -p "$OLDROOT/scripts" "$OLDROOT/registry"
git init -q "$OLDROOT" 2>/dev/null || true
cp "$ROOT/scripts/lib_mux.py" "$OLDROOT/scripts/"

if git -C "$SRC_DIR" show origin/main:scripts/watchdog.py > "$OLDROOT/scripts/watchdog.py" 2>/dev/null; then
  sed -i \
    -e 's/^TERMINATE_GRACE_PERIOD = .*/TERMINATE_GRACE_PERIOD = 2/' \
    -e 's/^KILL_DELAY = .*/KILL_DELAY = 1/' \
    "$OLDROOT/scripts/watchdog.py"

  setup_task 3 60
  # -a で mtime を保つ。-r だと activity/heartbeat が「今」の mtime になり、
  # 対照実験の前提 (無音 60s) が消えて意味の無い比較になる。
  cp -a "$ROOT/queue" "$OLDROOT/queue"
  cp -a "$ROOT/registry/activity" "$ROOT/registry/heartbeats" "$OLDROOT/registry/"
  spawn_pane idle

  CREWVIA_QUEUE="$OLDROOT/queue" python3 "$OLDROOT/scripts/watchdog.py" \
    --repo-root "$OLDROOT" --interval 1 >"$OLDROOT/watchdog.stderr" 2>&1 &
  OLDWD=$!
  sleep 8
  kill "$OLDWD" 2>/dev/null; wait "$OLDWD" 2>/dev/null

  OLDLOG="$OLDROOT/registry/watchdog.log"
  if grep -q 'TERMINATE: E2EWorker/t001' "$OLDLOG" 2>/dev/null; then
    bad "origin/main が terminate した (対照が成立していない)"
  else
    ok "origin/main は無音 60s / しきい値 3s でも terminate しない (= 欠陥の再現)"
    info "origin/main のログ行数: $(wc -l < "$OLDLOG" 2>/dev/null || echo 0) (起動行のみ)"
  fi
  pane_alive && ok "origin/main では Worker が生き残る" \
             || bad "origin/main なのに Worker が死んだ"

  # 非空振りの確認 — origin/main が「そもそも監視していなかった」ために
  # terminate しなかっただけなら、この対照は何も示していない。observations に
  # 「hard idle を超えているのに alive と判定した」記録が残っていることを要求する。
  OLDOBS="$OLDROOT/registry/watchdog-observations.jsonl"
  if python3 - "$OLDOBS" <<'PYEOF'
import json, sys
try:
    rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
except OSError:
    sys.exit(1)
hits = [r for r in rows
        if r.get("task_id") == "t001"
        and r.get("check_result") == "alive"
        and r.get("idle_seconds", 0) > 2 * r.get("idle_threshold", 10 ** 9)]
if not hits:
    sys.exit(1)
print(f'{len(hits)} 件 / 最大 idle={max(h["idle_seconds"] for h in hits)}s '
      f'threshold={hits[0]["idle_threshold"]}s')
PYEOF
  then
    ok "対照は空振りでない (origin/main は hard idle 超過を alive と判定していた)"
  else
    bad "対照が空振り — origin/main は監視自体をしていない。比較として無効"
  fi
  cleanup_pane
else
  info "SKIP: origin/main を解決できない (git fetch origin main が必要)"
fi

# ---------------------------------------------------------------------------

echo
echo "===================================="
echo "Results: PASS=$PASS FAIL=$FAIL"
echo "隔離環境 (消さずに残す): $ROOT"
echo "===================================="
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
