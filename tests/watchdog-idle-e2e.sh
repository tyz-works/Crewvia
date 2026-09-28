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
  "$ROOT/scripts/watchdog.py"

# B1 (#27): プロセス層の分類は lib_pane_process.py に移った。t065 (Codex review
# 2巡目) で判定根拠が時刻 (PROCESS_WORK_START_GRACE) から comm の同定に変わり、
# この定数は撤去された。t074 (Codex review 3巡目) で comm の同定も撤去し、
# 「祖先の cmdline に Bash tool / Monitor の shell snapshot wrapper
# (`/shell-snapshots/snapshot-`) が現れるか」に変わった (t082 で本ファイルの
# fixture をそれに追従させた — 追従できていなかったこと自体が今回の findings)。
cp "$SRC_DIR/scripts/lib_pane_process.py" "$ROOT/scripts/lib_pane_process.py"

# t082 (Codex review 4巡目 P2-3): watchdog.py はこの e2e が最後に更新されて以来
# lib_retirement / lib_daemon_watch / lib_daemon_state / lib_task_cards に依存が
# 増えており (retirement marker・通知台帳・task カード読み取りの入口)、コピー
# していなかったため `ModuleNotFoundError` で起動直後に落ちていた (このファイル
# がどの CI にも載っていないので誰も気付けなかった、という finding そのもの)。
# mux backend に触れないので、これらは (lib_pane_process.py と同じく) 実物を
# そのままコピーする — 偽物にするのは lib_mux.py だけでよい。
cp "$SRC_DIR/scripts/lib_retirement.py" "$ROOT/scripts/lib_retirement.py"
cp "$SRC_DIR/scripts/lib_daemon_watch.py" "$ROOT/scripts/lib_daemon_watch.py"
cp "$SRC_DIR/scripts/lib_daemon_state.py" "$ROOT/scripts/lib_daemon_state.py"
cp "$SRC_DIR/scripts/lib_task_cards.py" "$ROOT/scripts/lib_task_cards.py"

# claude 本体 / MCP サーバー相当を模す偽バイナリ (中身は sh / sleep のまま
# 機能する)。t074 以降、分類は comm を見ないのでこの名前自体は判定に効かない
# — ただの読みやすさのため。**cmdline に wrapper marker を含めないことが
# 唯一の条件**(下の fake bin / runner はどれもマーカーを持たない)。
#
# t097 (Codex review 6巡目 P1): セッション本体 (claude 相当) は symlink ではなく
# **実体ファイル**として `.../share/claude/versions/<ver>` に置く。symlink だと
# `/proc/<pid>/exe` はさらに先の実体 (`/bin/sh`) を指してしまい
# `lib_pane_process.SESSION_EXE_MARKER` (`/share/claude/versions/`) に一致しない
# (本番の `~/.local/bin/claude` → `~/.local/share/claude/versions/<version>` という
# symlink→実体ファイルの形をそのまま再現する必要がある)。
FAKE_BIN_DIR="$ROOT/fake-bin"
mkdir -p "$FAKE_BIN_DIR"
CLAUDE_VERSIONS_DIR="$ROOT/share/claude/versions"
mkdir -p "$CLAUDE_VERSIONS_DIR"
cp /bin/sh "$CLAUDE_VERSIONS_DIR/9.9.9"
chmod +x "$CLAUDE_VERSIONS_DIR/9.9.9"
ln -sf /bin/sleep "$FAKE_BIN_DIR/node"

# Bash tool / Monitor が実際に生成する wrapper の形 (t074 実測) を再現するための
# 使い捨て snapshot ファイル。中身は無害な no-op — 分類はパスの文字列
# (`/shell-snapshots/snapshot-`) しか見ない。
SNAPSHOT_DIR="$ROOT/.claude/shell-snapshots"
mkdir -p "$SNAPSHOT_DIR"
SNAPSHOT_FILE="$SNAPSHOT_DIR/snapshot-e2e-fixture.sh"
printf ': # no-op fixture snapshot\n' > "$SNAPSHOT_FILE"

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


# t082 (Codex review 4巡目 P2-3): watchdog.py が import する lib_daemon_watch.py
# は本物をコピーしており (mux backend に触れないので偽物にする必要が無い)、
# それが `from lib_mux import (...)` でこれらの名前を要求する。使わなくても
# モジュール読み込み時に ImportError になるので、実物と同じ値のスタブを置く。
class MuxTestIsolationError(RuntimeError):
    pass


MUX_TEST_ISOLATION_EXIT = 3
OWNER_MINE, OWNER_FOREIGN = "mine", "foreign"
OWNER_NONE, OWNER_UNKNOWN = "none", "unknown"


def pane_script_owner(pane_pid, script, mine, **kwargs):
    # e2e はこの関数を実際には呼ばない (watchdog.py の import 解決のためだけの
    # スタブ)。呼ばれたら気付けるよう、素通しにせず明示的に未実装とする。
    raise NotImplementedError("pane_script_owner is a stub for watchdog-idle-e2e.sh")


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
  # t082 (Codex review 4巡目 P2-3): t044 の「floor」(`monitoring_since = min(pulled_at,
  # self.started_at)`) は、card に使える `started_at` が無いと `self.started_at`
  # (= この watchdog プロセスがこの Worker を発見した時刻、ほぼ「今」) を使う。
  # activity ファイルの mtime をどれだけ過去にしても、floor が「今」だと
  # idle_seconds は実時間の経過分しか伸びない — この e2e が前提にしていた
  # 「あらかじめ古くしたファイルで即座に idle と判定される」が成立しない
  # (floor が無かった t016 時代の前提のまま止まっていた)。`started_at` を
  # 十分過去にして floor を下げ、activity の mtime がそのまま idle_seconds に
  # 反映されるようにする。
  local started_at
  started_at="$(date -u -d "@$(( $(date +%s) - activity_age - 3600 ))" '+%Y-%m-%dT%H:%M:%SZ')"
  cat > "$ROOT/queue/missions/e2e/tasks/t001.md" <<TEOF
---
id: t001
status: in_progress
worker: E2EWorker
started_at: ${started_at}
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
  # t082 (Codex review 4巡目 P2-3): この e2e が書かれた (t016) 後に、terminate
  # verdict の実行経路が retirement marker 方式 (t002: dispatcher が書き、
  # watchdog が実行する) に変わった。既定 (CREWVIA_KILL_AUTHORITY=watchdog) だと
  # `retirement.request()` → `bash plan.sh retire ...` を経由し、この隔離環境には
  # 無い mission.yaml / started_at の世代一致 / plan.sh 一式が要る (この e2e が
  # 検証したいプロセス層の判定そのものとは無関係)。`dispatcher` (rollback)
  # モードは「t002 以前と同じ」graceful_terminate() の直接呼び出しに戻るので、
  # ここではそちらを使う。CREWVIA_DAEMON_MUTUAL_WATCH=0 は dispatcher/watchdog
  # の相互監視 (この e2e には無関係) が「dispatcher を spawn しようとして失敗する」
  # ノイズを出すのを止める (偽 Mux に spawn() が無い)。
  CREWVIA_KILL_AUTHORITY=dispatcher CREWVIA_DAEMON_MUTUAL_WATCH=0 \
    python3 "$ROOT/scripts/watchdog.py" --repo-root "$ROOT" --interval 1 \
    >"$ROOT/watchdog.stderr" 2>&1 &
  local wd=$!
  sleep "$seconds"
  kill "$wd" 2>/dev/null
  wait "$wd" 2>/dev/null
}

# 監視対象の「ペイン」を立てる。
#   idle 木      : root -> claude(exe が share/claude/versions/ 実体, CLAUDECODE
#                  無し) -> node(comm, CLAUDECODE=1), node(comm, CLAUDECODE=1)
#                  (t097。claude 自身は session として判定から外れる)
#   executing 木 : root -> node(comm) / Bash tool wrapper が直接の子 (t082 時点の
#                  ままセッション層を模していないが、direct child の exe は
#                  どちらも SESSION_EXE_MARKER に一致しないため t097 の分岐は
#                  素通りし、以前と同じ判定になる)
#
# t097 (Codex review 6巡目 P1): idle 木のセッション役 (claude_bin) は本番同様
# CLAUDECODE を持たない (`_is_session_body()` が exe で同定して判定から外す)。
# 以前はここも explicit に CLAUDECODE=1 を与えていて、セッションが `unknown`
# に化ける欠陥を隠していた。
#
# t082 (Codex review 4巡目 P2-3): この fixture は t074 (comm → 起動元への
# 作り直し) に追従できておらず、素の `sh`/`sleep` (t065 時代の comm ベース
# fixture) のままだった — 新しい判定はそれを正しく idle_process と分類する
# ため、後続の `hard_idle_but_executing` assertion が落ちる (どの CI にも
# 載っていない e2e だったので、作り直しで壊れても誰も気付けなかった)。
spawn_pane() {
  local kind="$1"
  local pidfile="$ROOT/pane.pid"
  local claude_bin="$CLAUDE_VERSIONS_DIR/9.9.9"
  local node_bin="$FAKE_BIN_DIR/node"
  local runner="$ROOT/spawn_pane_runner.sh"
  local session_script="$ROOT/spawn_pane_session.sh"
  rm -f "$pidfile"
  # t091: environ を明示的に curate する。この e2e 自体が Claude Code の
  # Bash tool から (= Worker として) 走らせられると、その Bash tool 自身が
  # CLAUDE_CODE_CHILD_SESSION=1 を持つ (2026-09-28 実測) ため、明示しないと
  # "idle" 側の木までこれを継承して job に誤判定される
  # (classify_process_tree は environ も見るようになった — job の証拠が
  # `exec` で cmdline から消えても environ で拾うため)。
  # 入れ子の引用符地獄を避けるため、起動スクリプトを一時ファイルに書く。
  if [[ "$kind" == "executing" ]]; then
    cat > "$runner" <<EOF
#!/bin/sh
echo \$\$ > "$pidfile"
env -i PATH="\$PATH" CLAUDECODE=1 "$node_bin" 300 &
env -i PATH="\$PATH" CLAUDECODE=1 CLAUDE_CODE_CHILD_SESSION=1 CLAUDE_CODE_EXECPATH=/fake \
  bash -c "source '$SNAPSHOT_FILE' 2>/dev/null || true && eval 'sleep 300'" &
wait
EOF
  else
    # t097 (Codex review 6巡目 P1): セッション本体 (claude_bin) 自身の environ に
    # は CLAUDECODE を与えない (本番実測: herdr server が起動時点で消し、
    # 誰も書き戻さない)。CLAUDECODE は MCP 相当の子だけに明示的に与える —
    # 別ファイルに書いた session_script を claude_bin (実体は /bin/sh) に
    # 渡して実行させることで、claude_bin プロセス自身の起動コマンドラインに
    # は環境変数の代入を含めない (`-c "VAR=1 cmd"` だと VAR は子にしか効かない
    # ので実害は無いが、意図を読みやすくするため分離する)。
    cat > "$session_script" <<EOF
#!/bin/sh
env -i PATH="\$PATH" CLAUDECODE=1 "$node_bin" 300 &
env -i PATH="\$PATH" CLAUDECODE=1 "$node_bin" 300 &
wait
EOF
    chmod +x "$session_script"
    cat > "$runner" <<EOF
#!/bin/sh
echo \$\$ > "$pidfile"
env -i PATH="\$PATH" "$claude_bin" "$session_script" &
wait
EOF
  fi
  chmod +x "$runner"
  # setsid でプロセスグループを分けておくと、後片付けでグループごと殺せる。
  # 「ペインのシェル」の PID はランナー自身が echo $$ で書く ($! では setsid
  # 自身を掴みうるため確実ではない)。
  setsid "$runner" &
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
# シナリオ 4: 対照 — origin/main (B1 が無い版) は実行中の Worker も殺してしまう
# ---------------------------------------------------------------------------
#
# t082 (Codex review 4巡目 P2-3): この対照は元々「idle 木ですら terminate しない
# origin/main」を見せていたが、それはこの e2e が書かれた t016 の直後の話で、
# 現在の origin/main は t016 の修正 (idle 判定はプロセス層と独立に常に評価する)
# を既に持つ。plain な idle 木 (子孫がインフラだけ) は origin/main でも正しく
# terminate される (実測済み — 対照として何も示さない)。
#
# **B1 (この PR) が実際に足すもの**は「裏で本物の job (Bash tool / Monitor) が
# 走っている Worker を、hard idle だからといって殺さない」という保護であり、
# origin/main には (lib_pane_process.py 自体が無いので) この保護が丸ごと無い。
# ここでは executing 木 (シナリオ 3 と同じ、本物の job が生きている) を
# origin/main に食わせ、**実行中の Worker を殺してしまう**ことを示す —
# これがこの PR が無いと起きる実害そのものである。
echo
echo "[4] 対照: origin/main (B1 無し) は実行中の Worker も terminate してしまう"
OLDROOT="$ROOT/old"
mkdir -p "$OLDROOT/scripts" "$OLDROOT/registry"
git init -q "$OLDROOT" 2>/dev/null || true
cp "$ROOT/scripts/lib_mux.py" "$OLDROOT/scripts/"
# origin/main の watchdog.py も (B1 の lib_pane_process.py は無いが)
# lib_retirement / lib_daemon_watch / lib_daemon_state / lib_task_cards には
# 既に依存している (t002 のリタイアマーカー方式は main に先に入っている)。
# origin/main **自身の**版を使う — 現ブランチのコピーを流用しない (対照実験が
# 確かめたいのは「起動元判定を持ち込む前の watchdog.py」であって、その依存 lib
# まで現ブランチの版にするとその区別が曖昧になる)。
for lib in lib_retirement.py lib_daemon_watch.py lib_daemon_state.py lib_task_cards.py; do
  git -C "$SRC_DIR" show "origin/main:scripts/$lib" > "$OLDROOT/scripts/$lib" 2>/dev/null
done

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
  spawn_pane executing
  sleep 3   # シナリオ3と同じく、遅れて生える子孫が出そろうまで

  CREWVIA_QUEUE="$OLDROOT/queue" CREWVIA_KILL_AUTHORITY=dispatcher \
    CREWVIA_DAEMON_MUTUAL_WATCH=0 python3 "$OLDROOT/scripts/watchdog.py" \
    --repo-root "$OLDROOT" --interval 1 >"$OLDROOT/watchdog.stderr" 2>&1 &
  OLDWD=$!
  sleep 8
  kill "$OLDWD" 2>/dev/null; wait "$OLDWD" 2>/dev/null

  # t082 (Codex review 4巡目 P2-3): `registry/watchdog.log` という固定パスは
  # `today_log()` (実際のログ規約: `logs/watchdog/watchdog-<date>.log`、$ROOT
  # 基準) と食い違っていた — ログは実際にはここに正しく書かれていたのに、
  # 誤ったパスを見て「監視していなかった」と誤診していた。
  OLDLOG="$OLDROOT/logs/watchdog/watchdog-$(date +%Y%m%d).log"
  # 非空振りの確認: そもそも監視していなかった (verdict 行が無い) だけなら
  # この対照は何も示していない。hard_idle の verdict 行が実在することを要求する
  # (origin/main には `hard_idle_but_executing` という reason 自体が無い —
  # B1 が無いので process_signal を見て warn に落とす分岐が無い)。
  if grep -q 'reason=hard_idle' "$OLDLOG" 2>/dev/null; then
    ok "対照は空振りでない (origin/main も hard idle を検出していた)"
    info "$(grep -m1 'reason=hard_idle' "$OLDLOG")"
  else
    bad "対照が空振り — origin/main は監視自体をしていない。比較として無効"
    info "log: $(tail -5 "$OLDLOG" 2>/dev/null)"
  fi
  if grep -q 'TERMINATE: E2EWorker/t001' "$OLDLOG" 2>/dev/null; then
    ok "origin/main は実行中の Worker を terminate した (= B1 が無いとこの実害が起きる)"
  else
    bad "origin/main が terminate しなかった (対照が成立していない)"
  fi
  pane_alive && bad "origin/main なのに実行中の Worker が生き残った (対照が成立していない)" \
             || ok "origin/main では実行中の Worker が実際に殺された (欠陥の再現)"
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
