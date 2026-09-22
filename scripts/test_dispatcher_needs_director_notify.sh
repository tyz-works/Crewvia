#!/usr/bin/env bash
# test_dispatcher_needs_director_notify.sh — needs_director → Director 通知の回帰テスト (t027)
#
# 背景: needs_director に落ちた task を検知して Director に知らせる経路が dispatcher.sh
# に存在しなかった (grep で 'needs_director' が 0 件)。mission 20260921-daemon-authority
# で 10h28m の全停止が実際に発生した。この経路を追加した block を検証する。
#
# 検証内容:
#   1. dispatcher.sh に needs_director 検知 block が存在する (notify_key prefix)
#   2. dispatcher.sh の block は all_tasks (既走査済みの結果) を再利用しており、
#      ミッション単位の再スキャンを増やしていない
#   3. メッセージ構築ロジック (reason 1 行目 + 全文パス + reset コマンド) の単体検証
#   4. reason が空でも '(理由未記載)' でメッセージが壊れない
#   5. TTL dedup: 初回は通知、TTL 内は抑制、異なる task_id は独立
#   6. failed / pending など他ステータスの task は対象にならない
#   7. (t030) 通知本文が案内する復旧コマンドを実際に plan.sh update へ渡して実行し、
#      task が dispatch 可能な状態 (status=pending, worker=null) になることを assert
#      する。文字列一致だけでは検出できない不具合 (--status in_progress --reset だと
#      --reset 適用後に --status が上書きし in_progress/worker=null のまま固着する)
#      が過去に実在したため、コマンドの実行結果を検証する。
#
# t031 (PR #207 Seo review 差し戻し): Test 3-6 は元々このファイル内で dispatcher.sh の
# ロジック (build_msg / dedup / フィルタ) を再実装しており、dispatcher.sh 本体を一切
# 駆動していなかった。Seo は隔離コピーで dispatcher.sh の実際の文字列を
# `--status TOTAL-GARBAGE-NOT-A-STATUS` に差し替えても PASS=8/FAIL=0 のまま green に
# なることを実証した (再実装した期待値が壊れた実装と同じ壊れ方をしていたため)。
# また Test 7 も `RECOVERY_STATUS="pending"` をハードコードしており、dispatcher.sh の
# 通知文からステータス値を読んでいなかった (dispatcher.sh を `--status in_progress
# --reset` に戻しても PASS=12/FAIL=0 のまま通る)。
#
# 対応: dispatcher.sh の埋め込み Python サイクル本体 (`<<'PYEOF'` ... `PYEOF` の間 —
# 実際の daemon ループが 5 秒毎に実行しているのと同じ `publish_agents()` +
# `dispatch()`) を抽出し、tmux/herdr と通信する唯一の I/O 境界 (`_mux.send` /
# `_mux.list`、`_mux = Mux()` の直後) だけをスタブして送信メッセージをキャプチャする
# harness (run_dispatcher_cycle) に作り替えた。検知・dedup・メッセージ文言・復旧コマンド
# の組み立てはすべて dispatcher.sh の無改造コードが担うので、そこに regression が入れば
# キャプチャされるメッセージ自体が壊れ、以下の assertion が落ちる。
#
# 実行: bash scripts/test_dispatcher_needs_director_notify.sh
# 副作用: /tmp 配下に一時ファイル (queue fixture + git-init した空リポジトリ) を作成し
#         終了時に削除する。本番 dispatcher / registry には一切触れない (isolated
#         harness — knowledge/dispatcher-review.md 参照)。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCHER_SH="$OWN_CHECKOUT_ROOT/scripts/dispatcher.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  [[ -n "${TMPDIR_TEST:-}" && -d "$TMPDIR_TEST" ]] && rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

echo "== test_dispatcher_needs_director_notify.sh (t027: needs_director → Director 通知) =="

TMPDIR_TEST="/tmp/crewvia-test-needs-director-$$"
mkdir -p "$TMPDIR_TEST"

# ---------------------------------------------------------------------------
# Harness: drive the REAL dispatcher.sh cycle (not a hand-copy of its logic)
# ---------------------------------------------------------------------------

# extract_dispatcher_cycle <out.py> — pull the python body out of dispatcher.sh's
# `<<'PYEOF' ... PYEOF` heredoc (the code the bash while-loop at the bottom of
# dispatcher.sh feeds to `python3 -` every 5s).
extract_dispatcher_cycle() {
  awk '
    /<<.PYEOF.$/ { infile=1; next }
    infile && /^PYEOF$/ { exit }
    infile { print }
  ' "$DISPATCHER_SH" > "$1"
  if [[ ! -s "$1" ]]; then
    echo "ERROR: failed to extract dispatcher.sh python body (heredoc markers not found — dispatcher.sh structure changed?)" >&2
    return 1
  fi
}

# inject_mux_stub <extracted.py> <capture.json> — replace the mux transport
# (the only place the cycle talks to tmux/herdr) with an in-memory capture,
# right after `_mux = Mux()`. Everything above that line — detection, dedup,
# message text, recovery-command construction — stays dispatcher.sh's own code.
inject_mux_stub() {
  python3 - "$1" "$2" <<'INJECT'
import sys
path, capture_file = sys.argv[1], sys.argv[2]
src = open(path).read()
marker = "_mux = Mux()"
if marker not in src:
    print("ERROR: injection point '_mux = Mux()' not found in dispatcher.sh — harness needs updating", file=sys.stderr)
    sys.exit(1)
stub = (
    marker + "\n"
    "import json as _json_capture\n"
    "_SENT = []\n"
    "def _stub_send(name, text):\n"
    "    _SENT.append({'target': name, 'message': text})\n"
    "    return True\n"
    "_mux.send = _stub_send\n"
    # t032 F5 test needs a live Director by default (matches production's
    # common case) so Tests 1-7 keep exercising the notify-sent path; the
    # dedicated director-absent test below (inject_mux_stub_ext) overrides this.
    "_mux.list = lambda *a, suffix=None, **kw: (['Sora-director'] if suffix == '-director' else [])\n"
    "import atexit as _atexit_capture\n"
    f"_CAPTURE_FILE = {capture_file!r}\n"
    "_atexit_capture.register(lambda: open(_CAPTURE_FILE, 'w').write(_json_capture.dumps(_SENT)))\n"
)
src = src.replace(marker, stub, 1)
open(path, 'w').write(src)
INJECT
}

# inject_mux_stub_ext <extracted.py> <capture.json> <worker_names_csv> <director_names_csv> <state_value>
# Like inject_mux_stub, but lets a test control exactly which -worker / -director
# windows are "live" and what _mux.state()/_mux.capture() report — needed to drive
# check_rule5() (Rule 5), which inject_mux_stub's fixed empty '-worker' list never
# reaches (windows = tmux_list_worker_windows() short-circuits to [] otherwise).
inject_mux_stub_ext() {
  python3 - "$1" "$2" "$3" "$4" "$5" <<'INJECT'
import sys
path, capture_file, worker_csv, director_csv, state_value = sys.argv[1:6]
src = open(path).read()
marker = "_mux = Mux()"
if marker not in src:
    print("ERROR: injection point '_mux = Mux()' not found in dispatcher.sh — harness needs updating", file=sys.stderr)
    sys.exit(1)
worker_names = [n for n in worker_csv.split(',') if n]
director_names = [n for n in director_csv.split(',') if n]
stub = (
    marker + "\n"
    "import json as _json_capture\n"
    f"_WORKER_NAMES = {worker_names!r}\n"
    f"_DIRECTOR_NAMES = {director_names!r}\n"
    "_SENT = []\n"
    # Match HerdrBackend.send() (production backend): send() to a name whose
    # pane isn't live returns False (pane not found), it does not silently
    # succeed. Rule5/needs_director's "if tmux_send(...): ... else: log(...)"
    # fallback depends on this to detect a Director-absent send attempt.
    "def _stub_send(name, text):\n"
    "    if name not in _WORKER_NAMES and name not in _DIRECTOR_NAMES:\n"
    "        return False\n"
    "    _SENT.append({'target': name, 'message': text})\n"
    "    return True\n"
    "_mux.send = _stub_send\n"
    "def _stub_list(*a, suffix=None, **kw):\n"
    "    if suffix == '-worker':\n"
    "        return list(_WORKER_NAMES)\n"
    "    if suffix == '-director':\n"
    "        return list(_DIRECTOR_NAMES)\n"
    "    return []\n"
    "_mux.list = _stub_list\n"
    f"_STATE_VALUE = {state_value!r}\n"
    "_mux.state = lambda *a, **kw: _STATE_VALUE\n"
    "_mux.capture = lambda *a, **kw: ''\n"
    "import atexit as _atexit_capture\n"
    f"_CAPTURE_FILE = {capture_file!r}\n"
    "_atexit_capture.register(lambda: open(_CAPTURE_FILE, 'w').write(_json_capture.dumps(_SENT)))\n"
)
src = src.replace(marker, stub, 1)
open(path, 'w').write(src)
INJECT
}

# setup_fixture_repo <root> — a `git init`'d empty repo to serve as REPO_ROOT.
# dispatcher.sh refuses to run at all (repo_identity_ok() FATAL) unless its
# REGISTRY_DIR's parent is a real git checkout (has a `.git` entry) — a bare
# `git init` satisfies that check without needing real history/remote.
setup_fixture_repo() {
  local root="$1"
  mkdir -p "$root/registry"
  git init -q "$root" >/dev/null 2>&1
  cat > "$root/registry/workers.yaml" <<'EOF'
workers:
  - name: sofia
    skills: [bash]
    task_count: 0
EOF
}

# write_mission_fixture <queue_dir> <slug> — minimal active-mission scaffold.
write_mission_fixture() {
  local queue="$1" slug="$2"
  mkdir -p "$queue/missions/$slug/tasks" "$queue/archive"
  cat > "$queue/state.yaml" <<EOF
active_missions:
  - $slug
default_mission: $slug
EOF
  cat > "$queue/missions/$slug/mission.yaml" <<EOF
title: "Harness Test Mission"
slug: $slug
status: in_progress
created_at: 2026-09-22T00:00:00Z
completed_at: null
next_task_id: 99
EOF
}

# run_dispatcher_cycle <queue_dir> <registry_dir> <notify_cache> <capture.json>
# Runs exactly one real dispatch cycle (publish_agents() + dispatch(), the
# same pair the daemon's while-loop calls every 5s) against the given
# isolated fixture, with mux I/O captured to <capture.json>.
CYCLE_SEQ=0
run_dispatcher_cycle() {
  local queue="$1" registry="$2" notify_cache="$3" capture="$4"
  CYCLE_SEQ=$((CYCLE_SEQ + 1))
  local extracted="$TMPDIR_TEST/cycle_${CYCLE_SEQ}.py"
  extract_dispatcher_cycle "$extracted" || return 1
  inject_mux_stub "$extracted" "$capture" || return 1
  env -u TASKVIA_TOKEN CREWVIA_TASKVIA=disabled TASKVIA_URL= \
    PYTHONPATH="$OWN_CHECKOUT_ROOT/scripts" \
    python3 "$extracted" "$queue" "$registry" "$notify_cache" "300" "60" \
      "$TMPDIR_TEST/dispatcher-cycle.log" >> "$TMPDIR_TEST/dispatcher-cycle.log" 2>&1
}

# run_dispatcher_cycle_ext <queue> <registry> <notify_cache> <capture.json>
#   <worker_names_csv> <director_names_csv> <mux_state_value> <state_grace_seconds>
# Like run_dispatcher_cycle, but drives check_rule5() (Rule 5) too: lets the
# caller control which -worker/-director windows are live and what
# _mux.state() reports (Rule 5's A/B conditions), plus STATE_GRACE (arg 5 to
# dispatcher.sh) so grace-period waits don't require real wall-clock time in tests.
#
# Prints the per-cycle LOG_FILE path on stdout (so the caller can grep it for
# exact log-line-count assertions). Unlike run_dispatcher_cycle, the python
# process's own LOG_FILE arg is NOT the same path the shell redirects its
# stdout/stderr into — log() writes each line to both stderr AND LOG_FILE, so
# reusing one path for both would double-count every line.
run_dispatcher_cycle_ext() {
  local queue="$1" registry="$2" notify_cache="$3" capture="$4"
  local worker_csv="$5" director_csv="$6" state_value="$7" state_grace="$8"
  CYCLE_SEQ=$((CYCLE_SEQ + 1))
  local extracted="$TMPDIR_TEST/cycle_ext_${CYCLE_SEQ}.py"
  local logfile="$TMPDIR_TEST/dispatcher-cycle-ext-${CYCLE_SEQ}.log"
  extract_dispatcher_cycle "$extracted" || return 1
  inject_mux_stub_ext "$extracted" "$capture" "$worker_csv" "$director_csv" "$state_value" || return 1
  env -u TASKVIA_TOKEN CREWVIA_TASKVIA=disabled TASKVIA_URL= \
    PYTHONPATH="$OWN_CHECKOUT_ROOT/scripts" \
    python3 "$extracted" "$queue" "$registry" "$notify_cache" "300" "$state_grace" \
      "$logfile" >> "$TMPDIR_TEST/dispatcher-cycle.log" 2>&1
  echo "$logfile"
}

# captured_rule5_messages_for <capture.json> <task_id> — like captured_messages_for
# but restricted to "[Rule 5]" messages (needs_director/vanished_worker messages also
# contain "task <id> " and would otherwise collide in the t032 F4 tests below).
captured_rule5_messages_for() {
  python3 - "$1" "$2" <<'PYEOF'
import sys, json
capture_file, task_id = sys.argv[1], sys.argv[2]
try:
    text = open(capture_file).read().strip()
    msgs = json.loads(text) if text else []
except FileNotFoundError:
    msgs = []
# Rule 5's message shape is "(task <id>, mission=...)" — comma, not a space,
# right after the id (unlike the needs_director block's "task <id> (mission=...)").
needle = f"task {task_id},"
for m in msgs:
    text = m.get('message', '')
    if text.startswith('[Rule 5]') and needle in text:
        print(text)
PYEOF
}

# captured_messages_for <capture.json> <task_id> — print each captured
# message whose text mentions "task <task_id> ", one per line (jq-free).
captured_messages_for() {
  python3 - "$1" "$2" <<'PYEOF'
import sys, json
capture_file, task_id = sys.argv[1], sys.argv[2]
try:
    text = open(capture_file).read().strip()
    msgs = json.loads(text) if text else []
except FileNotFoundError:
    msgs = []
needle = f"task {task_id} "
for m in msgs:
    if needle in m.get('message', ''):
        print(m['message'])
PYEOF
}

# ---------------------------------------------------------------------------
# Test 1: dispatcher.sh に needs_director 検知 block が存在する
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 1: needs_director 検知 block の存在 --"
if grep -q "needs_director_{slug}_{task_id}" "$DISPATCHER_SH"; then
  pass "notify_key prefix 'needs_director_{slug}_{task_id}' が dispatcher.sh に存在する"
else
  fail "needs_director の notify_key が dispatcher.sh に見つからない"
fi

if grep -q "meta.get('status') != 'needs_director'" "$DISPATCHER_SH"; then
  pass "status == 'needs_director' の task を対象にするフィルタが存在する"
else
  fail "needs_director ステータスをフィルタする条件が見つからない"
fi

# ---------------------------------------------------------------------------
# Test 2: 既走査済みの all_tasks を再利用している (ミッション単位の再スキャンを増やさない)
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 2: all_tasks の再利用 (安価な判定) --"
needs_director_block=$(awk '/# needs_director detection:/{f=1} f{print} /# Handoff detection:/{exit}' "$DISPATCHER_SH")
if echo "$needs_director_block" | grep -q "for slug, meta in all_tasks:"; then
  pass "needs_director block は all_tasks をそのままイテレートしている"
else
  fail "needs_director block が all_tasks を再利用していない (再スキャンの疑い)"
fi
if echo "$needs_director_block" | grep -q "list_tasks_for_mission"; then
  fail "needs_director block が独自に list_tasks_for_mission() を呼んでいる (再スキャン増)"
else
  pass "needs_director block は追加のディレクトリスキャンを行っていない"
fi

# ---------------------------------------------------------------------------
# Test 3/4/6: 実際の dispatcher cycle を1回走らせ、複数ステータス・複数 reason 形状の
# task を同時に投入して「メッセージ構築」と「ステータスフィルタ」の両方を検証する。
# (旧 Test3/4/6 は build_msg() の再実装だった — F2 対応)
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 3/4/6: 実 dispatcher cycle によるメッセージ構築 + ステータスフィルタ --"

T346_ROOT="$TMPDIR_TEST/fixture-346"
T346_QUEUE="$T346_ROOT/queue"
T346_REPO="$T346_ROOT/fakerepo"
T346_SLUG="mission-346"
setup_fixture_repo "$T346_REPO"
write_mission_fixture "$T346_QUEUE" "$T346_SLUG"
T346_TASKS="$T346_QUEUE/missions/$T346_SLUG/tasks"

# t010: 単一行 reason
printf -- '---\nid: t010\ntitle: single-line\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: "Codex review NEEDS FIX: race in assign loop"\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t010.md"
# t011: reason が空
printf -- '---\nid: t011\ntitle: empty-reason\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: ""\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t011.md"
# t012: 200 文字超の長い reason (単一行)。plan.sh の書き込み側 (_dump_scalar) は
# 埋め込み改行を " / " に畳み込んで常に単一行で保存する (PR #181 — 生の改行を
# frontmatter に書くと parse_yaml がクラッシュする)。そのため
# needs_director_reason は実運用では絶対に複数物理行にならず、
# dispatcher.sh の `reason.splitlines()[0]` は事実上 no-op — 実際に省略記号を
# 発火させるのは後続の `[:200]` 文字数カットオフの方。HEAD_MARKER は先頭
# (200文字カットオフ内)、TAIL_MARKER はカットオフを超えた位置に置く。
T012_REASON="HEAD_MARKER-$(printf 'z%.0s' $(seq 1 210))-TAIL_MARKER"
printf -- '---\nid: t012\ntitle: long-reason\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: "%s"\n---\n\n## Description\nx\n' \
  "$T012_REASON" > "$T346_TASKS/t012.md"
# t013/t014/t015/t016: needs_director 以外のステータス (フィルタの対照例)
printf -- '---\nid: t013\ntitle: pending\nskills: [bash]\npriority: high\nstatus: pending\nblocked_by: []\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t013.md"
printf -- '---\nid: t014\ntitle: in-progress\nskills: [bash]\npriority: high\nstatus: in_progress\nblocked_by: []\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t014.md"
printf -- '---\nid: t015\ntitle: failed\nskills: [bash]\npriority: high\nstatus: failed\nblocked_by: []\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t015.md"
printf -- '---\nid: t016\ntitle: done\nskills: [bash]\npriority: high\nstatus: done\nblocked_by: []\n---\n\n## Description\nx\n' \
  > "$T346_TASKS/t016.md"

T346_NOTIFY_CACHE="$TMPDIR_TEST/notify-346.json"
T346_CAPTURE="$TMPDIR_TEST/capture-346.json"
run_dispatcher_cycle "$T346_QUEUE" "$T346_REPO/registry" "$T346_NOTIFY_CACHE" "$T346_CAPTURE"

if [[ ! -s "$T346_CAPTURE" ]]; then
  fail "dispatcher cycle がメッセージを1件もキャプチャしなかった (harness 自体が壊れている可能性)"
else
  MSG_T010=$(captured_messages_for "$T346_CAPTURE" t010)
  MSG_T011=$(captured_messages_for "$T346_CAPTURE" t011)
  MSG_T012=$(captured_messages_for "$T346_CAPTURE" t012)

  # Test 3: 単一行 reason はそのまま全文が入り、省略記号は付かない
  if [[ "$MSG_T010" == *"$T346_SLUG"* && "$MSG_T010" == *"Codex review NEEDS FIX: race in assign loop"* \
        && "$MSG_T010" == *"plan.sh update t010 --status pending --reset --mission $T346_SLUG"* ]]; then
    pass "メッセージ構築: 単一行 reason は全文がそのまま入り、復旧コマンドも組み立てられる"
  else
    fail "メッセージ構築: 単一行 reason (t010) の内容が期待と異なる: $MSG_T010"
  fi
  if [[ "$MSG_T010" != *$'\xe2\x80\xa6'* ]]; then
    pass "メッセージ構築: 単一行 reason (t010) に省略記号が付かない"
  else
    fail "メッセージ構築: 単一行 reason なのに省略記号が付いた: $MSG_T010"
  fi

  # Test 4: 空 reason は '(理由未記載)' になる
  if [[ "$MSG_T011" == *"(理由未記載)"* ]]; then
    pass "メッセージ構築: 空 reason は '(理由未記載)' になる"
  else
    fail "メッセージ構築: 空 reason (t011) で '(理由未記載)' が出ない: $MSG_T011"
  fi

  # Test 4: 200 文字超の長い reason は先頭 200 文字のみ + 省略記号 + 全文パス誘導
  if [[ "$MSG_T012" == *"HEAD_MARKER"* && "$MSG_T012" != *"TAIL_MARKER"* \
        && "$MSG_T012" == *"$T346_SLUG/tasks/t012.md"* && "$MSG_T012" == *$'\xe2\x80\xa6'* ]]; then
    pass "メッセージ構築: 200 文字超の reason は先頭のみ + 省略記号 + 全文パス誘導になる"
  else
    fail "メッセージ構築: 長い reason (t012) の処理が期待と異なる: $MSG_T012"
  fi

  # Test 6: needs_director 以外のステータスは対象にならない
  NON_TARGET_HIT=0
  for other_id in t013 t014 t015 t016; do
    hit=$(captured_messages_for "$T346_CAPTURE" "$other_id")
    [[ -n "$hit" ]] && NON_TARGET_HIT=1
  done
  if [[ "$NON_TARGET_HIT" -eq 0 ]]; then
    pass "needs_director 以外のステータス (pending/in_progress/failed/done) は通知対象にならない"
  else
    fail "needs_director フィルタが他ステータスの task を誤って拾っている"
  fi
fi

# ---------------------------------------------------------------------------
# Test 5: TTL dedup — 実 dispatcher cycle を複数回走らせて検証する
# (旧 Test5 は should()/record() の再実装だった — F2 と同型のため合わせて修正)
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 5: TTL dedup (実 dispatcher cycle を複数回走らせる) --"

T5_ROOT="$TMPDIR_TEST/fixture-5"
T5_QUEUE="$T5_ROOT/queue"
T5_REPO="$T5_ROOT/fakerepo"
T5_SLUG="mission-5"
setup_fixture_repo "$T5_REPO"
write_mission_fixture "$T5_QUEUE" "$T5_SLUG"
T5_TASKS="$T5_QUEUE/missions/$T5_SLUG/tasks"
printf -- '---\nid: t020\ntitle: stuck-a\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: "stuck a"\n---\n\n## Description\nx\n' \
  > "$T5_TASKS/t020.md"
printf -- '---\nid: t021\ntitle: stuck-b\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: "stuck b"\n---\n\n## Description\nx\n' \
  > "$T5_TASKS/t021.md"

T5_NOTIFY_CACHE="$TMPDIR_TEST/notify-5.json"

# Cycle 1: 初回 → 両方通知される (別 key は独立)
T5_CAP1="$TMPDIR_TEST/capture-5-1.json"
run_dispatcher_cycle "$T5_QUEUE" "$T5_REPO/registry" "$T5_NOTIFY_CACHE" "$T5_CAP1"
C1_T020=$(captured_messages_for "$T5_CAP1" t020)
C1_T021=$(captured_messages_for "$T5_CAP1" t021)
if [[ -n "$C1_T020" && -n "$C1_T021" ]]; then
  pass "TTL dedup: 初回サイクルで両方の needs_director task が通知される (別 key 独立)"
else
  fail "TTL dedup: 初回サイクルで通知が欠けている (t020 present=$([[ -n $C1_T020 ]] && echo y || echo n), t021 present=$([[ -n $C1_T021 ]] && echo y || echo n))"
fi

# Cycle 2: 同一 NOTIFY_CACHE で即再実行 → TTL 内なので両方抑制される
T5_CAP2="$TMPDIR_TEST/capture-5-2.json"
run_dispatcher_cycle "$T5_QUEUE" "$T5_REPO/registry" "$T5_NOTIFY_CACHE" "$T5_CAP2"
C2_T020=$(captured_messages_for "$T5_CAP2" t020)
if [[ -z "$C2_T020" ]]; then
  pass "TTL dedup: TTL 内 (300s) の再サイクルでは同一 needs_director 通知が抑制される"
else
  fail "TTL dedup: TTL 内に同一通知が繰り返された: $C2_T020"
fi

# TTL 経過をシミュレート: notify_cache の t020 key を TTL+1 秒前に書き換える
python3 - "$T5_NOTIFY_CACHE" "$T5_SLUG" <<'PYEOF'
import sys, json, time
cache_file, slug = sys.argv[1], sys.argv[2]
c = json.loads(open(cache_file).read())
key = f"needs_director_{slug}_t020"
assert key in c, f"expected key {key} not recorded after cycle 1: {list(c)}"
c[key] = time.time() - 301
open(cache_file, 'w').write(json.dumps(c))
PYEOF

# Cycle 3: TTL 経過後 → t020 は再送、t021 はまだ TTL 内なので抑制されたまま
T5_CAP3="$TMPDIR_TEST/capture-5-3.json"
run_dispatcher_cycle "$T5_QUEUE" "$T5_REPO/registry" "$T5_NOTIFY_CACHE" "$T5_CAP3"
C3_T020=$(captured_messages_for "$T5_CAP3" t020)
C3_T021=$(captured_messages_for "$T5_CAP3" t021)
if [[ -n "$C3_T020" && -z "$C3_T021" ]]; then
  pass "TTL dedup: TTL 経過後は再送され (t020)、TTL 内の別 task (t021) は抑制されたまま"
else
  fail "TTL dedup: TTL 経過後の再送に問題 (t020 present=$([[ -n $C3_T020 ]] && echo y || echo n), t021 present=$([[ -n $C3_T021 ]] && echo y || echo n))"
fi

# ---------------------------------------------------------------------------
# Test 7 (t030 / t031): 通知本文が案内する復旧コマンドを、実 dispatcher cycle の
# キャプチャから動的に抽出して実行し、task が dispatch 可能な状態
# (status=pending, worker=null) になることを assert する。
#
# t031 での修正: 旧版は RECOVERY_STATUS="pending" をハードコードしており、
# dispatcher.sh のメッセージから読んでいなかった。今回はキャプチャした本物の
# メッセージ文字列から `--status` 以降の全引数を正規表現で抜き出し、そのまま
# plan.sh update に渡す — dispatcher.sh が案内するステータス値が変わればこの
# テストも追随し、`--status in_progress --reset` の罠が戻れば pending
# assertion で確実に FAIL する。
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 7: 通知本文の復旧コマンドを動的抽出して実行し、実際に dispatch 可能な状態に戻ることを検証 --"

PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"
T7_ROOT="$TMPDIR_TEST/fixture-7"
T7_QUEUE="$T7_ROOT/queue"
T7_REPO="$T7_ROOT/fakerepo"
T7_SLUG="mission-7"
setup_fixture_repo "$T7_REPO"
write_mission_fixture "$T7_QUEUE" "$T7_SLUG"
T7_TASKS="$T7_QUEUE/missions/$T7_SLUG/tasks"
T7_TASK_ID="t001"
printf -- '---\nid: %s\ntitle: Stuck task\nskills: [bash]\npriority: medium\nstatus: needs_director\nblocked_by: []\nworker: sofia\nstarted_at: 2026-09-22T00:00:00Z\ncompleted_at: null\nneeds_director_reason: "stuck, need guidance"\n---\n\n## Description\nDo the thing.\n\n## Result\n' \
  "$T7_TASK_ID" > "$T7_TASKS/$T7_TASK_ID.md"

T7_NOTIFY_CACHE="$TMPDIR_TEST/notify-7.json"
T7_CAPTURE="$TMPDIR_TEST/capture-7.json"
run_dispatcher_cycle "$T7_QUEUE" "$T7_REPO/registry" "$T7_NOTIFY_CACHE" "$T7_CAPTURE"

T7_RECOVERY_ARGS=""
if [[ -s "$T7_CAPTURE" ]]; then
  T7_RECOVERY_ARGS=$(python3 - "$T7_CAPTURE" "$T7_TASK_ID" <<'PYEOF'
import sys, json, re
capture_file, task_id = sys.argv[1], sys.argv[2]
msgs = json.loads(open(capture_file).read())
target = next((m['message'] for m in msgs if f"task {task_id} " in m.get('message', '')), None)
if not target:
    sys.exit(1)
m = re.search(r'plan\.sh update (.+?) で差し戻してください', target)
if not m:
    sys.exit(1)
print(m.group(1))
PYEOF
)
fi

if [[ -n "$T7_RECOVERY_ARGS" ]]; then
  pass "dispatcher cycle が needs_director 通知を発火し、復旧コマンド引数を抽出できた: $T7_RECOVERY_ARGS"
else
  fail "dispatcher cycle から needs_director 通知の復旧コマンドを抽出できなかった"
fi

# T7_RECOVERY_ARGS は dispatcher.sh の実際のメッセージから抜き出した引数列
# (例: "t001 --status pending --reset --mission mission-7") — ハードコードしない。
if [[ -n "$T7_RECOVERY_ARGS" ]]; then
  # shellcheck disable=SC2086
  if CREWVIA_QUEUE="$T7_QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
     bash "$PLAN_SH" update $T7_RECOVERY_ARGS > /dev/null 2>&1; then
    pass "案内された plan.sh update コマンド (動的抽出) が exit 0 で完了する"
  else
    fail "案内された plan.sh update コマンド (動的抽出) が失敗した: update $T7_RECOVERY_ARGS"
  fi
else
  fail "復旧コマンドを抽出できなかったため plan.sh update を実行できない"
fi

T7_STATUS=$(awk -F': ' '/^status:/{print $2; exit}' "$T7_TASKS/$T7_TASK_ID.md" 2>/dev/null)
T7_WORKER=$(awk -F': ' '/^worker:/{print $2; exit}' "$T7_TASKS/$T7_TASK_ID.md" 2>/dev/null)

if [[ "$T7_STATUS" == "pending" ]]; then
  pass "復旧コマンド実行後、task の status が pending になっている (dispatch 対象)"
else
  fail "復旧コマンド実行後、status が pending になっていない (実際: '$T7_STATUS') — --status in_progress --reset の罠が再発している可能性"
fi

if [[ "$T7_WORKER" == "null" ]]; then
  pass "復旧コマンド実行後、worker が null になっている (pull 可能)"
else
  fail "復旧コマンド実行後、worker が null になっていない (実際: '$T7_WORKER')"
fi

# pull で実際に取得できる = dispatch 可能な状態であることの最終確認
if CREWVIA_QUEUE="$T7_QUEUE" CREWVIA_REPO_ROOT="$OWN_CHECKOUT_ROOT" \
   bash "$PLAN_SH" pull --agent test-worker --skills bash --mission "$T7_SLUG" > /dev/null 2>&1; then
  pass "復旧後の task は plan.sh pull で実際に取得できる (needs_director の罠が再発していない)"
else
  fail "復旧後の task を plan.sh pull で取得できない — 復旧コマンドが dispatch 不能な状態を作っている"
fi

# ---------------------------------------------------------------------------
# Test 8 (t032 F4): needs_director task の assignment に対して Rule 5 の
# idle-with-task (条件B) が発火しない (needs_director 通知と重複しない)。
# 対照として同一設定 (state=idle, assignment 有り, grace=0) の in_progress task では
# Rule 5 が発火することも確認し、「そもそも Rule 5 が発火しない harness」ではないことを
# 保証する (t031 で踏んだ「壊れた実装のまま green」の再発防止)。
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 8 (F4): needs_director task は Rule 5 idle-with-task と重複通知しない --"

T8_ROOT="$TMPDIR_TEST/fixture-8"
T8_QUEUE="$T8_ROOT/queue"
T8_REPO="$T8_ROOT/fakerepo"
T8_SLUG="mission-8"
setup_fixture_repo "$T8_REPO"
cat > "$T8_REPO/registry/workers.yaml" <<'EOF'
workers:
  - name: wei
    skills: [bash]
    task_count: 0
  - name: taro
    skills: [bash]
    task_count: 0
EOF
write_mission_fixture "$T8_QUEUE" "$T8_SLUG"
T8_TASKS="$T8_QUEUE/missions/$T8_SLUG/tasks"
mkdir -p "$T8_QUEUE/assignments"

# t040: needs_director, worker=wei (escalated — cmd_needs_director leaves the
# assignment file in place, this is exactly the F4 scenario).
printf -- '---\nid: t040\ntitle: escalated\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nworker: wei\nneeds_director_reason: "stuck f4"\n---\n\n## Description\nx\n' \
  > "$T8_TASKS/t040.md"
echo "$T8_SLUG:t040" > "$T8_QUEUE/assignments/wei"

# t041: in_progress, worker=taro — positive control, same idle/assignment shape.
printf -- '---\nid: t041\ntitle: still-working\nskills: [bash]\npriority: high\nstatus: in_progress\nblocked_by: []\nworker: taro\n---\n\n## Description\nx\n' \
  > "$T8_TASKS/t041.md"
echo "$T8_SLUG:t041" > "$T8_QUEUE/assignments/taro"

T8_NOTIFY_CACHE="$TMPDIR_TEST/notify-8.json"
T8_CAP1="$TMPDIR_TEST/capture-8-1.json"
T8_CAP2="$TMPDIR_TEST/capture-8-2.json"

# Cycle 1: both workers' Rule 5 state transitions are fresh — grace timer just
# started for both, so neither notifies yet (matches check_rule5's existing
# "condition changed → reset, don't notify" behaviour).
run_dispatcher_cycle_ext "$T8_QUEUE" "$T8_REPO/registry" "$T8_NOTIFY_CACHE" "$T8_CAP1" \
  "wei-worker,taro-worker" "Sora-director" "idle" "0" > /dev/null

# Cycle 2: grace(0) elapsed for both — t041 (in_progress) should now fire Rule 5;
# t040 (needs_director) must stay suppressed.
run_dispatcher_cycle_ext "$T8_QUEUE" "$T8_REPO/registry" "$T8_NOTIFY_CACHE" "$T8_CAP2" \
  "wei-worker,taro-worker" "Sora-director" "idle" "0" > /dev/null

R5_T040=$(captured_rule5_messages_for "$T8_CAP2" t040)
R5_T041=$(captured_rule5_messages_for "$T8_CAP2" t041)

if [[ -z "$R5_T040" ]]; then
  pass "needs_director task (t040) は Rule 5 idle-with-task を発火しない"
else
  fail "needs_director task (t040) なのに Rule 5 が発火した: $R5_T040"
fi

if [[ -n "$R5_T041" ]]; then
  pass "同一条件の in_progress task (t041, 対照) では Rule 5 が正しく発火する (harness 自体は機能している)"
else
  fail "対照 task (t041) でも Rule 5 が発火しなかった — harness が Rule 5 を全く駆動できていない疑い"
fi

# ---------------------------------------------------------------------------
# Test 9 (t032 F5): Director 不在時、needs_director block は Rule 5 と同じ
# director_live ガードで 1 行ログに留め、tmux_send を試行しない (ログ洪水防止)。
# 復旧 (Director が戻る) 後は抑制されず通知される (notify_key を記録していないため)。
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 9 (F5): Director 不在時、needs_director 通知は director_live ガードで抑制される --"

T9_ROOT="$TMPDIR_TEST/fixture-9"
T9_QUEUE="$T9_ROOT/queue"
T9_REPO="$T9_ROOT/fakerepo"
T9_SLUG="mission-9"
setup_fixture_repo "$T9_REPO"
write_mission_fixture "$T9_QUEUE" "$T9_SLUG"
T9_TASKS="$T9_QUEUE/missions/$T9_SLUG/tasks"
printf -- '---\nid: t050\ntitle: stuck-no-director\nskills: [bash]\npriority: high\nstatus: needs_director\nblocked_by: []\nneeds_director_reason: "stuck f5"\n---\n\n## Description\nx\n' \
  > "$T9_TASKS/t050.md"

T9_NOTIFY_CACHE="$TMPDIR_TEST/notify-9.json"

# Cycle 1: no Director window live at all.
T9_CAP1="$TMPDIR_TEST/capture-9-1.json"
T9_LOG1=$(run_dispatcher_cycle_ext "$T9_QUEUE" "$T9_REPO/registry" "$T9_NOTIFY_CACHE" "$T9_CAP1" \
  "" "" "idle" "60")

T9_MSG1=$(captured_messages_for "$T9_CAP1" t050)
if [[ -z "$T9_MSG1" ]]; then
  pass "Director 不在時、needs_director 通知は送信されない (tmux_send を試行しない)"
else
  fail "Director 不在なのに needs_director 通知が送信された: $T9_MSG1"
fi

T9_GUARD_LINES=$(grep -c "needs_director — Director 不在のため通知スキップ.*t050" "$T9_LOG1" || true)
T9_DOUBLE_LOG_LINES=$(grep -c "needs_director detected but mux send failed.*t050" "$T9_LOG1" || true)

if [[ "$T9_GUARD_LINES" -eq 1 ]]; then
  pass "Director 不在ガードのログが 1 行だけ出る (Rule 5 と同じ体裁)"
else
  fail "Director 不在ガードのログ行数が期待と異なる (期待 1, 実際 $T9_GUARD_LINES)"
fi

if [[ "$T9_DOUBLE_LOG_LINES" -eq 0 ]]; then
  pass "旧経路の 'mux send failed' ログ (2行目) は出ない — ログ洪水が解消されている"
else
  fail "旧経路の 'mux send failed' ログがまだ出ている ($T9_DOUBLE_LOG_LINES 行) — F5 未解消"
fi

# Cycle 2: Director comes back — notify_key was never recorded while suppressed,
# so it should re-arm immediately (no NOTIFY_TTL wait needed for recovery).
T9_CAP2="$TMPDIR_TEST/capture-9-2.json"
run_dispatcher_cycle_ext "$T9_QUEUE" "$T9_REPO/registry" "$T9_NOTIFY_CACHE" "$T9_CAP2" \
  "" "Sora-director" "idle" "60" > /dev/null
T9_MSG2=$(captured_messages_for "$T9_CAP2" t050)
if [[ -n "$T9_MSG2" ]]; then
  pass "Director 復帰後は即座に needs_director 通知が送信される (抑制中に notify_key を記録していない)"
else
  fail "Director 復帰後も needs_director 通知が送信されない"
fi

# ---------------------------------------------------------------------------
# 結果サマリ
# ---------------------------------------------------------------------------
echo ""
echo "== 結果: PASS=${PASS_COUNT}, FAIL=${FAIL_COUNT} =="

if [[ $FAIL_COUNT -eq 0 ]]; then
  echo "全テスト PASS"
  exit 0
else
  echo "失敗したテストがあります"
  exit 1
fi
