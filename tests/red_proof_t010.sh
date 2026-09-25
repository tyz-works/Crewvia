#!/usr/bin/env bash
# t010 (#10 + #11) の回帰テストが「欠陥を戻すと赤くなる」ことの実証。
#
# 使い方:
#     bash tests/red_proof_t010.sh
#
# 作業用のコピーに 1 つずつ欠陥を注入し、その欠陥を見張るはずのテストが赤になる
# (= 失敗する) ことを確かめる。緑のままなら、そのテストは欠陥の留め金になって
# いない (memory: regression-test-must-prove-red / red-proof-catches-tests-green-for-the-wrong-reason)。
#
#   M1  already_told が常に False            → 「TTL 後も 1 回だけ」が赤
#   M2  fingerprint が定数                    → 「入力が変わったら再通知」が赤
#   M3  prune_told が何もしない               → 「状態を離れて戻ったら再通知」が赤
#   M4  拒否記録を見ない (常に 'none')         → 「拒否済みは再 spawn しない」が赤
#   M5  読めない拒否記録を 'none' に倒す        → 「壊れた記録は spawn を保留」が赤
#   M6  台帳に書けない/壊れているときに黙る     → 「使えない台帳を声に出す」が赤
#   M7  kai-review.sh が拒否記録を書かない      → scripts/test_kai_review.sh が赤
#
# 隔離: 本番の worktree には触らない (使い捨てのコピーの中だけで変異させる)。
# $PYTHONDONTWRITEBYTECODE=1: 欠陥注入は .pyc を通して古い姿を拾わせない
# (memory: defect-injection-needs-pyc-purge)。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t010.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1

cleanup() { chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null; }
trap cleanup EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  OK:   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  BAD:  $1"; }

fresh_copy() {
  rm -rf "$WORK/tree"; mkdir -p "$WORK/tree"
  rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
        --exclude='node_modules' --exclude='logs' \
        "$REPO_ROOT/scripts" "$REPO_ROOT/tests" "$REPO_ROOT/config" "$WORK/tree/"
}

# mutate <file> <python-expression-over-s>  — 置換できなかったら注入失敗として止まる。
mutate() {
  python3 - "$WORK/tree/$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
if old not in s:
    print(f"INJECTION FAILED: pattern not found in {path}: {old[:70]!r}", file=sys.stderr)
    sys.exit(3)
open(path, "w").write(s.replace(old, new, 1))
PYEOF
}

run_pytest() { (cd "$WORK/tree" && python3 -m pytest tests/test_dispatcher_notify_once.py -q --no-header -p no:cacheprovider 2>&1); }

expect_red() {   # <label> <output> <substring of a test name that must fail>
  local label="$1" out="$2" name="$3"
  if echo "$out" | grep -q "^FAILED .*${name}"; then
    ok "$label → ${name} が赤になった"
  else
    bad "$label → ${name} が赤にならなかった (欠陥の留め金になっていない)"
    echo "$out" | tail -5
  fi
}

echo "== baseline: 変異なしで緑"
fresh_copy
out="$(run_pytest)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"; else bad "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== M1: already_told が常に False"
fresh_copy
mutate scripts/dispatcher.sh "    if is_unreadable(told):
        return False
    entry = told.get(key)" "    return False
    entry = told.get(key)" || { bad "M1 注入失敗"; }
out="$(run_pytest)"
expect_red M1 "$out" "test_needs_director_is_told_once_even_after_the_throttle_expires"
expect_red M1 "$out" "test_handoff_is_told_once_even_after_the_throttle_expires"

echo "== M2: fingerprint が定数"
fresh_copy
mutate scripts/dispatcher.sh "    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]" "    return 'constant'" || bad "M2 注入失敗"
out="$(run_pytest)"
expect_red M2 "$out" "test_needs_director_is_told_again_when_the_reason_changes"
expect_red M2 "$out" "test_handoff_is_told_again_when_the_handoff_path_changes"

echo "== M3: prune_told が何もしない"
fresh_copy
mutate scripts/dispatcher.sh "    told = load_told()
    if is_unreadable(told):
        return
    stale =" "    return
    stale =" || bad "M3 注入失敗"
out="$(run_pytest)"
expect_red M3 "$out" "test_needs_director_left_and_re_entered_with_same_reason_is_told_again"
expect_red M3 "$out" "test_handoff_left_and_re_entered_is_told_again"

echo "== M4: 拒否記録を見ない"
fresh_copy
mutate scripts/dispatcher.sh "    task_id = meta.get('id', '?')
    try:
        rec = lib_review_refusal.load(" "    return 'none', None
    task_id = meta.get('id', '?')
    try:
        rec = lib_review_refusal.load(" || bad "M4 注入失敗"
out="$(run_pytest)"
expect_red M4 "$out" "test_a_refused_codex_review_task_is_not_respawned"
expect_red M4 "$out" "test_refusal_tells_the_director_to_review_by_hand_once"

echo "== M5: 読めない拒否記録を 'none' に倒す"
fresh_copy
mutate scripts/dispatcher.sh "    if is_unreadable(rec):
        return 'unreadable', rec" "    if is_unreadable(rec):
        return 'none', None" || bad "M5 注入失敗"
out="$(run_pytest)"
expect_red M5 "$out" "test_unreadable_refusal_holds_the_spawn"

echo "== M6: 使えない台帳を黙って読み飛ばす (声に出さない)"
fresh_copy
mutate scripts/dispatcher.sh "    if should_notify('told_ledger_trouble'):" "    if False:" || bad "M6 注入失敗"
out="$(run_pytest)"
expect_red M6 "$out" "test_unwritable_store_degrades_to_the_throttle_and_says_so"
expect_red M6 "$out" "test_corrupt_store_is_reported_then_repaired_by_the_next_successful_record"

echo "== M7: kai-review.sh が拒否記録を書かない"
fresh_copy
mutate scripts/kai-review.sh "  if [[ \$DRY_RUN -eq 0 && -n \"\$MISSION_SLUG\" ]]; then" "  if false; then" || bad "M7 注入失敗"
kout="$(cd "$WORK/tree" && timeout 900 bash scripts/test_kai_review.sh 2>&1)"
if echo "$kout" | grep -q "REGRESSION (t010/#11)"; then
  ok "M7 → scripts/test_kai_review.sh の拒否記録テストが赤になった"
else
  bad "M7 → 拒否記録テストが赤にならなかった"
  echo "$kout" | tail -5
fi

echo
echo "== 結果: OK=$PASS BAD=$FAIL"
[[ $FAIL -eq 0 ]]
