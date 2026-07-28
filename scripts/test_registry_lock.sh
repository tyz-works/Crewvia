#!/usr/bin/env bash
# test_registry_lock.sh — task_161 回帰テスト
#
# 不具合: agents/worker.md:476 が registry/workers.yaml を
# read_text() -> 正規表現置換 -> write_text() という生の全文読み書きで更新して
# おり、ロックが無かった。lib_registry.py 経由の3経路(register-director /
# set-last-active / assign-name.sh)と並行に走ると、worker.md:476 側が古い
# スナップショットに基づいて全文書込を行い、その間に他経路が追加した新規
# エントリを消してしまう(lost update)。task_161 で実機再現・是正済み。
#
# 修正: registry/workers.yaml への read-modify-write を全て lib_registry.py
# の with_lock() (fcntl.flock、scripts/plan.sh の with_lock と同じ流儀) の
# もとで実行するよう統一した。worker.md:476 の生のファイル手術は撤去し、
# 新設の lib_registry.py bump-task-count 経由に置き換えた。
#
# このテストは3つを検証する:
#   1. 静的検査 — lib_registry.py を経由しない workers.yaml への生の
#      read_text/write_text 呼び出しがリポジトリ内に存在しないこと
#      (取り忘れ・将来の再迂回を検出する構造的な関所)
#   2. 機能スモークテスト — with_lock でラップした後も register-director /
#      set-last-active / bump-task-count が従来どおり動作すること
#   3. 実プロセス間のロック競合テスト — 2つの実OSプロセスを実際に並行実行し、
#      lost update が起きないこと、かつ実際に flock でブロックしていることを
#      タイムスタンプで実測する
#
# 実行: bash scripts/test_registry_lock.sh
# 副作用: 一時ディレクトリに合成データを作成し、終了時に必ず削除する
#         (crewvia の実 registry/workers.yaml には一切触れない)。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Top level of THIS checkout (test_handoff_path.sh と同じ理由: ブランチ依存の
# 内容を検証するには、このスクリプト自身が存在する checkout を見る必要がある)
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

echo "== test_registry_lock.sh (task_161 regression test) =="

# ---------------------------------------------------------------------------
# 1. 静的検査: lib_registry.py を経由しない workers.yaml への生の write_text
#    呼び出しが無いこと。
#
# ★read_text は対象外(読取専用の同時アクセスはlost updateの原因にならない
# — 実際 dispatcher.sh / verifier-dispatcher.sh / hooks/post-tool-use.sh は
# 正当にworkers.yamlを読取専用で参照しており、当初read_text も検査対象に
# 含めたところこれら3ファイルが誤検知した。書込のみが並行書込レースの原因
# であるため write_text のみを検査対象にした)。
#
# スコープを意図的に絞った理由(task_158 W-1 と同じ判断): 全面的な "workers.yaml"
# 文字列 grep にすると、単に PATH をコマンドライン引数として lib_registry.py へ
# 渡しているだけの正当な呼び出し(assign-name.sh の変数名や worker.md の CLI
# 呼び出し行)まで誤検知してしまう。過去に実際に発生したバグの形
# (Python pathlib の .write_text( を workers.yaml と同一ファイル内で使っている)
# にピンポイントで絞ることで、誤検知ゼロで意図した迂回を検出する。
# ★shell の `>` リダイレクトは検査対象に含めていない — assign-name.sh には
# レジストリ未作成時の初期化(`printf 'workers: []\n' > "$REGISTRY_YAML"`)という
# 正当な既存コードがあり、これはレース対象ではない(対象ファイルが存在しない
# 時にのみ走る一回きりの初期化であり、並行書込の相手が存在し得ない)。
# ★限界: 単純な「同一ファイル内co-occurrence」では dispatcher.sh /
# verifier-dispatcher.sh を誤検知した(両方とも先頭付近で WORKERS_FILE =
# .../workers.yaml を定義しつつ、遠く離れた箇所で全く別の変数(NOTIFY_CACHE)に
# write_text している)。そのため「workers.yaml への言及」と「write_text(」が
# 近接行(±N行)に共起する場合のみを検査対象にする行番号ベースの照合に変更した
# (worker.md:476の実際のバグ形は数行〜十数行以内に両方が現れていた)。
# それでも対象そのものを追跡する完全な静的解析ではなく、あくまでヒューリス
# ティックであることに留意。
echo ""
echo "-- 1. static check: lib_registry.py 以外に生の write_text は無いか --"
BYPASS_OUT="$(python3 - "$OWN_CHECKOUT_ROOT" <<'PYEOF'
import sys, re, pathlib

root = pathlib.Path(sys.argv[1])
exclude = {
    root / "scripts" / "lib_registry.py",
    root / "scripts" / "test_registry_lock.sh",
}
proximity = 15
found = []

for path in root.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix not in (".py", ".sh", ".md"):
        continue
    if path in exclude:
        continue
    if ".git" in path.parts:
        continue
    if ".claude" in path.parts and "worktrees" in path.parts:
        continue
    try:
        lines = path.read_text().splitlines()
    except Exception:
        continue
    yaml_lines = [i for i, l in enumerate(lines) if "workers.yaml" in l]
    write_lines = [i for i, l in enumerate(lines) if re.search(r"\.write_text\(", l)]
    if not yaml_lines or not write_lines:
        continue
    for wl in write_lines:
        if any(abs(wl - yl) <= proximity for yl in yaml_lines):
            found.append(f"{path}:{wl + 1}")
            break

for f in found:
    print(f)
PYEOF
)"

if [[ -n "$BYPASS_OUT" ]]; then
  while IFS= read -r line; do
    fail "bypass detected: $line references workers.yaml near a raw write_text call"
  done <<< "$BYPASS_OUT"
else
  pass "no raw write_text bypass of workers.yaml found outside lib_registry.py"
fi

# ---------------------------------------------------------------------------
# 2. 機能スモークテスト: with_lock でラップ後も従来どおり動作するか
# ---------------------------------------------------------------------------
echo ""
echo "-- 2. functional smoke test (register-director / set-last-active / bump-task-count) --"

TMPDIR_TEST="$(mktemp -d /tmp/task161_registry_lock_test.XXXXXX)"
REGISTRY_TMP="$TMPDIR_TEST/workers.yaml"
cat > "$REGISTRY_TMP" <<'EOF'
# synthetic test fixture (test_registry_lock.sh)
workers:
  - name: SmokeAlice
    role: director
    skills: []
    task_count: 3
    last_active: 2026-07-20
  - name: SmokeBob
    role:
    skills: [research]
    task_count: 1
    last_active: 2026-07-27
EOF

python3 "$SCRIPT_DIR/lib_registry.py" register-director "$REGISTRY_TMP" "SmokeCarol" >/dev/null 2>&1
if grep -q 'name: SmokeCarol' "$REGISTRY_TMP" && grep -A1 'name: SmokeCarol' "$REGISTRY_TMP" | grep -q 'role: director'; then
  pass "register-director still adds a new director entry correctly"
else
  fail "register-director did not produce the expected entry"
fi

python3 "$SCRIPT_DIR/lib_registry.py" set-last-active "$REGISTRY_TMP" "SmokeBob" "2026-08-01" >/dev/null 2>&1
if grep -A3 'name: SmokeBob' "$REGISTRY_TMP" | grep -q 'last_active: 2026-08-01'; then
  pass "set-last-active still updates last_active correctly"
else
  fail "set-last-active did not update the expected field"
fi

python3 "$SCRIPT_DIR/lib_registry.py" bump-task-count "$REGISTRY_TMP" "SmokeBob" >/dev/null 2>&1
if grep -A2 'name: SmokeBob' "$REGISTRY_TMP" | grep -q 'task_count: 2'; then
  pass "bump-task-count increments task_count correctly (worker.md:476 replacement)"
else
  fail "bump-task-count did not increment task_count as expected"
fi

if [[ -f "$TMPDIR_TEST/.workers.lock" ]]; then
  pass "lock file created as a sibling of the registry (with_lock is actually engaged)"
else
  fail "no lock file created — with_lock may not be wired up"
fi

# ---------------------------------------------------------------------------
# 3. 実プロセス間のロック競合テスト
# ---------------------------------------------------------------------------
echo ""
echo "-- 3. real concurrent-process lock contention test --"

REGISTRY_RACE="$TMPDIR_TEST/race_workers.yaml"
cat > "$REGISTRY_RACE" <<'EOF'
# synthetic test fixture (test_registry_lock.sh, race test)
workers:
  - name: RaceAlice
    role: director
    skills: []
    task_count: 3
    last_active: 2026-07-20
  - name: RaceBob
    role:
    skills: [research]
    task_count: 1
    last_active: 2026-07-27
EOF

PROC_B_PY="$TMPDIR_TEST/procB.py"
cat > "$PROC_B_PY" <<'PYEOF'
import sys, time, os
from datetime import date
sys.path.insert(0, sys.argv[1])
from lib_registry import parse, write, with_lock
REGISTRY_PATH = sys.argv[2]
HOLD_SECONDS = float(sys.argv[3])
t_start = time.time()

def _do():
    time.sleep(HOLD_SECONDS)
    header, order, by_name = parse(REGISTRY_PATH)
    by_name['RaceBob']['task_count'] = by_name['RaceBob'].get('task_count', 0) + 1
    by_name['RaceBob']['last_active'] = str(date.today())
    write(REGISTRY_PATH, header, order, by_name)

with_lock(REGISTRY_PATH, _do)
PYEOF

PROC_A_PY="$TMPDIR_TEST/procA.py"
cat > "$PROC_A_PY" <<'PYEOF'
import sys, time, os
from datetime import date
sys.path.insert(0, sys.argv[1])
from lib_registry import parse, write, with_lock
REGISTRY_PATH = sys.argv[2]
t_start = time.time()

def _do():
    t_acquired = time.time()
    print(f"WAITED={t_acquired - t_start:.3f}")
    header, order, by_name = parse(REGISTRY_PATH)
    by_name['NewWorkerRace'] = {
        'name': 'NewWorkerRace', 'role': '', 'skills': ['quick'],
        'task_count': 0, 'last_active': str(date.today()),
    }
    order.append('NewWorkerRace')
    write(REGISTRY_PATH, header, order, by_name)

with_lock(REGISTRY_PATH, _do)
PYEOF

HOLD_SECONDS=2
python3 "$PROC_B_PY" "$SCRIPT_DIR" "$REGISTRY_RACE" "$HOLD_SECONDS" &
PROC_B_PID=$!
sleep 0.3
PROC_A_OUT="$(python3 "$PROC_A_PY" "$SCRIPT_DIR" "$REGISTRY_RACE")"
wait "$PROC_B_PID"

WAITED_SECONDS="$(echo "$PROC_A_OUT" | grep -oE '[0-9.]+' | head -1)"
WAITED_OK=0
if [[ -n "$WAITED_SECONDS" ]]; then
  # procA should have waited close to (HOLD_SECONDS - 0.3s head start), i.e. >1s.
  # A loose >1.0s threshold avoids flakiness while still proving real blocking
  # occurred (no lock would mean waited ~0.0s).
  WAITED_OK="$(python3 -c "print(1 if float('$WAITED_SECONDS') > 1.0 else 0)")"
fi
if [[ "$WAITED_OK" == "1" ]]; then
  pass "procA genuinely blocked on procB's held lock (waited ${WAITED_SECONDS}s, real OS-level flock contention)"
else
  fail "procA did not appear to block on the lock (waited=${WAITED_SECONDS:-<none>}s) — lock may not be effective"
fi

if grep -q 'name: NewWorkerRace' "$REGISTRY_RACE" && grep -A2 'name: RaceBob' "$REGISTRY_RACE" | grep -q 'task_count: 2'; then
  pass "no lost update: both concurrent writers' changes survived (RaceBob task_count=2 AND NewWorkerRace present)"
else
  fail "lost update detected post-fix: concurrent writes did not both survive"
fi

echo ""
echo "test_registry_lock.sh: PASS=$PASS_COUNT FAIL=$FAIL_COUNT"
[[ "$FAIL_COUNT" -eq 0 ]]
