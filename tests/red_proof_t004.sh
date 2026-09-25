#!/usr/bin/env bash
# `plan.sh fail` の証拠要求 (t004) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t004.sh
#
#   baseline — いまの木では tests/test_fail_evidence.py が緑
#   case A   — fail が検証入口を通らない (元の欠陥そのもの)      → 赤
#   case B   — done が検証入口を通らない (逆向きの非対称)        → 赤
#   case C   — FAIL の規則が PASS の検証 (QA Gate) を流用する    → 赤 (FAIL 不能 outage の形)
#   case D   — handoff と head の結び付きを外す (古い再提出)     → 赤
#   case E   — update --reset が前回の証拠を持ち越す              → 赤
#   case F   — 相対 handoff パスを受け付ける (Kai P2: 基準の食い違い) → 赤
#   case G   — reset が相対パスを cwd 基準で解く (dispatcher と不一致) → 赤
#   case H   — 退避先が衝突したら黙って上書きする (Kai P2)           → 赤
#   case I   — 変数経由の自動 fail 呼び出しを注入 (QA F1)            → 赤
#            (旧ガード = リテラル `plan.sh fail` の正規表現は、同じ注入で緑のまま)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree の plan.sh には触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t004.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

fresh_copy() {
    rm -rf "$WORK/tree"; mkdir -p "$WORK/tree"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$WORK/tree/"
}

# inject <old> <new> — 複製した plan.sh の old を new に置換 (1 か所だけ。無ければ FATAL)
inject() {
    OLD="$1" NEW="$2" python3 - "$WORK/tree/scripts/plan.sh" <<'PY' || { echo "FATAL: 注入点が見つからない"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# inject_in <相対パス> <old> <new> — plan.sh 以外の複製ファイルへの注入
inject_in() {
    OLD="$2" NEW="$3" python3 - "$WORK/tree/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() { ( cd "$WORK/tree" && python3 -m pytest tests/test_fail_evidence.py -q -p no:cacheprovider 2>&1 ); }

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
if run_suite | grep -q " passed" && ! run_suite | grep -q "failed"; then ok "baseline は緑"; else ng "baseline が緑でない"; fi

echo "== case A: fail が入口を通らない"
fresh_copy
inject "        err, fields = _gate_terminal_report('fail', meta, {
            'head': opts.get('--head'),
            'no_head': opts.get('--no-head'),
            'handoff_path': handoff_path,
        })" "        err, fields = None, {'fail_head': None, 'fail_head_waiver': 'red-proof'}"
expect_red "case A" "test_done_and_fail_go_through_the_same_gate"
expect_red "case A (挙動)" "test_fail_without_head_is_rejected"

echo "== case B: done が入口を通らない"
fresh_copy
inject "        err, _fields = _gate_terminal_report(
            'done', meta, {'result': result, 'task_id': task_id})" "        err = None"
expect_red "case B" "test_every_terminal_report_writer_uses_the_gate"
expect_red "case B (挙動)" "test_done_on_a_declared_task_is_still_gated"

echo "== case C: FAIL が PASS の検証を流用する"
fresh_copy
inject "    head = report.get('head')
    no_head = report.get('no_head')" "    _validate_qa_gate('', True)
    head = report.get('head')
    no_head = report.get('no_head')"
expect_red "case C" "test_gate_treats_fail_with_its_own_rule_not_the_pass_rule"

echo "== case D: handoff と head の結び付きが無い"
fresh_copy
inject "    path = os.path.abspath(handoff_path)
    text = _TASK_CARDS" "    return True, None
    path = os.path.abspath(handoff_path)
    text = _TASK_CARDS"
expect_red "case D" "test_a_stale_handoff_from_another_head_is_rejected"

echo "== case E: reset が前回の証拠を持ち越す"
fresh_copy
inject "            for stale_key in ('handoff_path', 'fail_head', 'fail_head_waiver'):" "            for stale_key in ():"
expect_red "case E" "test_reset_clears_the_previous_failure_evidence"

echo "== case F: 相対 handoff パスを受け付ける"
fresh_copy
inject "    if handoff_path and not os.path.isabs(handoff_path):" "    if False:"
expect_red "case F" "test_a_relative_handoff_path_is_rejected"

echo "== case G: reset が相対パスを cwd 基準で解く"
fresh_copy
inject "real = os.path.realpath(os.path.join(repo_root, handoff_path))" "real = os.path.realpath(handoff_path)"
expect_red "case G" "test_reset_resolves_a_relative_handoff_path_like_the_dispatcher"

echo "== case H: 退避先の衝突を検出せず上書きする"
fresh_copy
inject "target = _reserve_unique_path(f\"{real}.stale-{stamp}\")" "target = f\"{real}.stale-{stamp}\""
expect_red "case H" "test_set_aside_never_overwrites_an_earlier_report"

echo "== case I: 変数経由の自動 fail 呼び出し (lib_retirement.py の実際の argv 形)"
fresh_copy
inject_in scripts/lib_retirement.py 'argv = ["bash", str(self.plan_sh), "retire", task_id,' 'argv = ["bash", str(self.plan_sh), "fail", task_id,'
expect_red "case I" "test_no_automated_caller_invokes_fail_without_a_decision"
# 旧ガード (リテラル正規表現) が同じ注入を見逃すことの確認 = この case が F1 の再現になっている証拠
if python3 - "$WORK/tree/scripts/lib_retirement.py" <<'PY'
import re, sys
old = re.compile(r"""(?x)(?: plan(?:\.sh)?["']? \s+ fail\b | ["']plan(?:\.sh)?["'] \s*,\s* ["']fail["'] | \bcmd_fail\b )""")
sys.exit(0 if not old.search(open(sys.argv[1]).read()) else 1)
PY
then ok "case I: 旧ガードの正規表現は同じ注入を検出できない (= F1 の再現)"; else ng "case I: 旧正規表現でも検出できてしまう (注入が F1 を再現していない)"; fi

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
