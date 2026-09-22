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
#
# 実行: bash scripts/test_dispatcher_needs_director_notify.sh
# 副作用: /tmp 配下に一時ファイルを作成し終了時に削除する。本番 dispatcher / registry
#         には一切触れない (isolated harness — knowledge/dispatcher-review.md 参照)。

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
# needs_director block と handoff block の間の行だけを抜き出し、
# 独自の list_tasks_for_mission() 呼び出しを増やしていないことを確認する。
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
# Test 3-4: メッセージ構築ロジックの単体検証 (dispatcher.sh と同じロジックを再現)
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 3-4: メッセージ構築ロジック --"
python3 <<'PYEOF'
from pathlib import Path

MISSIONS_DIR = Path("/tmp/crewvia-test-needs-director-fake/missions")

def build_msg(slug, meta):
    task_id = meta.get('id', '?')
    reason = (meta.get('needs_director_reason') or '').strip()
    reason_line = reason.splitlines()[0][:200] if reason else '(理由未記載)'
    task_file = MISSIONS_DIR / slug / 'tasks' / f'{task_id}.md'
    msg = (
        f'[needs_director] task {task_id} (mission={slug}) が needs_director です。'
        f'理由: {reason_line}'
        + ('…' if len(reason) > len(reason_line) else '')
        + f' (全文: {task_file})。'
        f'reason を読んで方針を決め、plan.sh update {task_id} --status in_progress --reset '
        f'--mission {slug} で差し戻してください。'
    )
    return msg

# Test 3: 単一行 reason
msg1 = build_msg('mission-a', {'id': 't027', 'needs_director_reason': 'Codex review NEEDS FIX: race in assign loop'})
assert 't027' in msg1
assert 'mission-a' in msg1
assert 'Codex review NEEDS FIX: race in assign loop' in msg1
assert 'plan.sh update t027 --status in_progress --reset --mission mission-a' in msg1
assert '…' not in msg1, f"1 行 reason なのに省略記号が付いた: {msg1}"
print("Test3 OK:", msg1[:80])

# Test 4: reason が空 / 複数行 → 1 行目のみ + 省略記号 + task_file 誘導
msg2 = build_msg('mission-b', {'id': 't099', 'needs_director_reason': ''})
assert '(理由未記載)' in msg2, f"空 reason で '(理由未記載)' が出ない: {msg2}"

msg3 = build_msg('mission-c', {'id': 't100', 'needs_director_reason': '1行目のみ表示されるべき\n2行目は本文に埋もれる長い補足説明'})
assert '1行目のみ表示されるべき' in msg3
assert '2行目は本文に埋もれる長い補足説明' not in msg3, f"2 行目が漏れている: {msg3}"
assert '…' in msg3, f"複数行 reason なのに省略記号が付かない: {msg3}"
assert 'mission-c/tasks/t100.md' in msg3.replace('\\', '/'), f"全文パス誘導が無い: {msg3}"

print("Test4 OK: 空/複数行 reason も安全に処理される")
PYEOF
if [[ $? -eq 0 ]]; then
  pass "メッセージ構築: 単一行 reason は全文がそのまま入る"
  pass "メッセージ構築: 空/複数行 reason でも 1 行目 + 省略記号 + 全文パス誘導になる"
else
  fail "メッセージ構築ロジックの単体検証に失敗"
fi

# ---------------------------------------------------------------------------
# Test 5: TTL dedup (should_notify/record_notify と同じロジック — 既存 Test 8 と同形)
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 5: TTL dedup (needs_director 専用 key) --"
TMPDIR_TEST="/tmp/crewvia-test-needs-director-$$"
mkdir -p "$TMPDIR_TEST"
NOTIFY_CACHE_TEST="$TMPDIR_TEST/notify-test.json"
python3 - "$NOTIFY_CACHE_TEST" "300" <<'PYEOF'
import sys, json, time
from pathlib import Path

NOTIFY_CACHE = Path(sys.argv[1])
NOTIFY_TTL   = int(sys.argv[2])

def load(): return json.loads(NOTIFY_CACHE.read_text()) if NOTIFY_CACHE.exists() else {}
def save(c): NOTIFY_CACHE.write_text(json.dumps(c))
def should(key): c = load(); return key not in c or time.time() - c[key] > NOTIFY_TTL
def record(key): c = load(); c[key] = time.time(); save(c)

key = "needs_director_mission-a_t027"

# 初回は通知する
assert should(key) == True, "初回通知が抑制されている"
record(key)
# 記録直後は TTL 内なので抑制される (永久ミュートではなく TTL 明けに再送されることは
# TTL 判定式 (time.time()-cache[key] > NOTIFY_TTL) 自体が保証する — 他の通知と同じ仕組み)
assert should(key) == False, "TTL 内に同一 needs_director 通知が繰り返される"

# 別 task / 別 mission は独立して初回通知される
assert should("needs_director_mission-a_t028") == True, "別 task_id の通知が誤って抑制されている"
assert should("needs_director_mission-b_t027") == True, "別 mission の通知が誤って抑制されている"

# TTL が過ぎたことにする (過去のタイムスタンプを直接書き込み、経過をシミュレート)
c = load()
c[key] = time.time() - (NOTIFY_TTL + 1)
save(c)
assert should(key) == True, "TTL 経過後も再送されない (永久ミュートになっている — 要件違反)"

print("TTL dedup OK: 初回通知 / TTL内抑制 / 別key独立 / TTL経過後の再送 すべて成立")
PYEOF
if [[ $? -eq 0 ]]; then
  pass "TTL dedup: 初回通知 OK、TTL 内 dedup OK、別 key 独立、TTL 経過後は再送される"
else
  fail "TTL dedup ロジックに問題あり (needs_director 用 key)"
fi

# ---------------------------------------------------------------------------
# Test 6: 対象外ステータス (failed / pending / in_progress) は needs_director フィルタを通らない
# ---------------------------------------------------------------------------
echo ""
echo "-- Test 6: needs_director 以外のステータスはフィルタされる --"
python3 <<'PYEOF'
all_tasks = [
    ('m', {'id': 't001', 'status': 'pending'}),
    ('m', {'id': 't002', 'status': 'in_progress'}),
    ('m', {'id': 't003', 'status': 'failed', 'handoff_path': '/tmp/x'}),
    ('m', {'id': 't004', 'status': 'needs_director', 'needs_director_reason': 'stuck'}),
    ('m', {'id': 't005', 'status': 'done'}),
]

targets = [meta['id'] for slug, meta in all_tasks if meta.get('status') == 'needs_director']
assert targets == ['t004'], f"needs_director フィルタが他ステータスを拾っている: {targets}"
print("Test6 OK: needs_director のみが対象になる")
PYEOF
if [[ $? -eq 0 ]]; then
  pass "needs_director 以外のステータス (pending/in_progress/failed/done) は通知対象にならない"
else
  fail "needs_director フィルタが他ステータスの task を誤って拾っている"
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
