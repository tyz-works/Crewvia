#!/usr/bin/env bash
# plan.sh の --result-file / --notes-file (C1) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_c1_result_file.sh
#
#   baseline — いまの木では tests/test_plan_result_file.py が緑
#   case A   — ファイル option を無視する (--result-file が無かった頃の形)          → 赤
#   case B   — 標準入力の退避 (fd 3) を外す                                      → 赤 (`-` が読めない)
#   case C   — 位置引数との併用を黙って受ける                                    → 赤
#   case D   — 空の本文を受け入れる                                              → 赤
#   case E   — 標準入力の非 UTF-8 を置換して受け入れる                           → 赤
#   case F   — 読めないファイルを空文字にして進む (読めない = 空に潰す)           → 赤
#   case G   — 本文を shell 展開する (eval 相当の欠陥)                           → 赤 (sentinel が作られる)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の plan.sh には触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-c1.XXXXXX")"
trap 'chmod -R u+rwX "$WORK" 2>/dev/null; mv "$WORK" "$WORK.done" 2>/dev/null' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
N=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

TREE=""
fresh_copy() {
    N=$((N + 1)); TREE="$WORK/tree$N"; mkdir -p "$TREE"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.claude' \
          --exclude='queue' --exclude='logs' "$REPO_ROOT/" "$TREE/"
}

# inject <old> <new> — 複製した plan.sh の old を new に置換 (1 か所だけ。無ければ FATAL)
inject() {
    OLD="$1" NEW="$2" python3 - "$TREE/scripts/plan.sh" <<'PY' || { echo "FATAL: 注入点が見つからない"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

run_suite() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_plan_result_file.py -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -q "failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: ファイル option を無視する"
fresh_copy
inject '    path = opts.get(file_flag)
    if path is None:
        return inline' '    path = None
    if path is None:
        return inline'
expect_red "case A" "test_result_file_keeps_the_body_verbatim_and_runs_nothing"

echo "== case B: fd 3 の退避を外す"
fresh_copy
inject 'if ! { exec 3<&0; } 2>/dev/null; then
  exec 3</dev/null
fi' ':'
expect_red "case B" "test_result_from_stdin_keeps_the_body_verbatim_and_runs_nothing"

echo "== case C: 併用を黙って受ける"
fresh_copy
inject '    if inline is not None:
        _usage_exit(f"{file_flag} と {inline_desc} は同時に指定できません")' '    pass'
expect_red "case C" "test_positional_and_file_together_is_rejected_and_writes_nothing"

echo "== case D: 空の本文を受け入れる"
fresh_copy
inject '    if not text.strip():
        _usage_exit(f"{file_flag} {path}: 本文が空です (空の Result を黙って記録しない)")' '    pass'
expect_red "case D" "test_done_rejects_unusable_result_file_and_writes_nothing"

echo "== case E: 標準入力の非 UTF-8 を置換して受ける"
fresh_copy
inject "            text = raw.decode('utf-8')" "            text = raw.decode('utf-8', 'replace')"
expect_red "case E" "test_stdin_empty_or_non_utf8_is_rejected_and_writes_nothing"

echo "== case F: 読めないファイルを空文字にして進む (空の検査も外す)"
fresh_copy
inject '        if _TASK_CARDS.is_unreadable(text):
            _usage_exit(f"{file_flag} {path}: 読めません ({text.reason})")
    if not text.strip():
        _usage_exit(f"{file_flag} {path}: 本文が空です (空の Result を黙って記録しない)")' "        if _TASK_CARDS.is_unreadable(text):
            text = ''
"
expect_red "case F" "test_done_rejects_unusable_result_file_and_writes_nothing"

echo "== case G: 本文を shell 展開する"
fresh_copy
inject "    return text.rstrip('\\n')" "    return subprocess.run(['bash', '-c', 'eval echo \"\$1\"', 'x', text], capture_output=True, text=True).stdout.rstrip('\\n')"
expect_red "case G" "test_result_file_keeps_the_body_verbatim_and_runs_nothing"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
