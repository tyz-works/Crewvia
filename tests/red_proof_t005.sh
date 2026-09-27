#!/usr/bin/env bash
# plan.sh の厳格な引数 (t005 / backlog #23) が、欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t005.sh
#
#   baseline — いまの木では tests/test_plan_strict_args.py が緑
#   case A   — 修正前 (origin/main 版) の plan.sh に差し替える            → 赤 (全体)
#   case B   — `--help` / 使い方の誤りのあとにも task-graph を再生成する   → 赤 (registry に書く)
#   case C   — queue の骨組みを引数の検証より前に作る                     → 赤 (--help が作る)
#   case D   — 未知の option を positional に混ぜる (元の欠陥)             → 赤
#   case E   — `--` を特別扱いしない                                      → 赤
#   case F   — env の mission を、worker の一致を見ずに使う                → 赤
#   case G   — env の mission を、status を見ずに使う                      → 赤
#   case H   — pull の使い方の誤りが exit 2 (= 「タスクなし」と同じ)       → 赤
#   case I   — Director を `ROLE` 環境変数でも判定する                     → 赤
#   case J   — skills が無いとき絞り込みなしで進む (元の欠陥)              → 赤
#   case K   — pull --task の複数 mission を拒否しない                     → 赤
#   case L   — positional の最大数の宣言を 1 つ落とす                      → 赤
#   case M   — dashboard: queue の骨組みを引数の検証より前に作る (t035)     → 赤
#   case N   — dashboard: -h / --help を usage にしない                    → 赤
#   case O   — dashboard: 未知の option を受理する                         → 赤
#   case P   — bash 側に新しいサブコマンドを足し、usage 行に載せない        → 赤 (列挙の構造テスト)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree の plan.sh には触らない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
# 複製は毎回新しい mktemp -d に作り、削除はしない (rm -rf を使わない。終了時に .done へ退避)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t005.XXXXXX")"
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

# 呼び出し元の AGENT_NAME / SKILLS / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_suite() {
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_plan_strict_args.py -q -p no:cacheprovider 2>&1 )
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

echo "== case A: 修正前の plan.sh"
fresh_copy
if git -C "$REPO_ROOT" show origin/main:scripts/plan.sh > "$TREE/scripts/plan.sh" 2>/dev/null \
   && ! grep -q "UsageExit" "$TREE/scripts/plan.sh"; then
    out="$(run_suite)"
    if echo "$out" | grep -qE "[0-9]+ failed"; then ok "case A → 赤 ($(echo "$out" | tail -1))"
    else ng "case A → 赤にならなかった"; echo "$out" | tail -5; fi
else
    echo "  SKIP: origin/main が修正済み (または取得できない) ので、修正前の版を作れない"
fi

echo "== case B: --help のあとにも task-graph を再生成する"
fresh_copy
inject "if _exit_code_of(_e) != LOCK_BUSY and not isinstance(_e, UsageExit):" "if _exit_code_of(_e) != LOCK_BUSY:"
expect_red "case B" "test_help_writes_nothing_in_a_populated_queue"

echo "== case C: 骨組みを引数の検証より前に作る"
fresh_copy
inject "    opts = {}
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--':" "    _ensure_queue_dirs()
    opts = {}
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--':"
expect_red "case C" "test_help_prints_usage_and_writes_nothing_on_a_fresh_directory"

echo "== case D: 未知の option を positional に混ぜる"
fresh_copy
inject "        elif _OPTION_LIKE.match(a):
            hint = \"\"" "        elif False:
            hint = \"\""
expect_red "case D" "test_done_with_an_agent_flag_no_longer_writes_the_flag_as_the_result"

echo "== case E: -- を特別扱いしない"
fresh_copy
inject "        if a == '--':
            positional.extend(args[i + 1:])
            break" "        if False:
            break"
expect_red "case E" "test_double_dash_makes_the_rest_positional"

echo "== case F: env の mission を worker の一致を見ずに使う"
fresh_copy
inject "    if meta.get('worker') != agent or meta.get('status') not in _ENV_MISSION_STATUSES:" "    if meta.get('status') not in _ENV_MISSION_STATUSES:"
expect_red "case F" "test_env_mission_of_another_worker_is_not_enough"

echo "== case G: env の mission を status を見ずに使う"
fresh_copy
inject "    if meta.get('worker') != agent or meta.get('status') not in _ENV_MISSION_STATUSES:" "    if meta.get('worker') != agent:"
expect_red "case G" "test_env_mission_with_the_agents_own_name_on_a_finished_card_is_not_enough"

echo "== case H: pull の使い方の誤りが exit 2"
fresh_copy
inject "PULL_USAGE_EXIT = 1" "PULL_USAGE_EXIT = 2"
expect_red "case H" "test_unknown_option_is_rejected_and_nothing_is_written\[pull\]"

echo "== case I: Director を ROLE 環境変数でも判定する"
fresh_copy
inject "    if registered and registered.get('role', '').strip('\"\\' ').lower() == 'director':" "    if (registered and registered.get('role', '').strip('\"\\' ').lower() == 'director') or os.environ.get('ROLE', '').lower() == 'director':"
expect_red "case I" "test_role_env_is_not_consulted"

echo "== case J: skills が無いとき絞り込みなしで進む"
fresh_copy
inject "    if not requested_skills:
        die(\"pull requires the worker's skills:" "    if False:
        die(\"pull requires the worker's skills:"
expect_red "case J" "test_pull_without_any_skills_is_rejected_and_writes_nothing"

echo "== case K: pull --task の複数 mission を拒否しない"
fresh_copy
inject "            if len(holders) > 1:" "            if False:"
expect_red "case K" "test_pull_task_in_two_missions_without_mission_is_rejected"

echo "== case L: positional の最大数の宣言を 1 つ落とす"
fresh_copy
inject "'status': (0, 0), " ""
expect_red "case L" "test_too_many_positionals_are_rejected_not_silently_dropped\[status\]"

# --- dashboard (t035 / t006 QA の FAIL): python の dispatch を通らない bash 側の分岐 -------------

echo "== case M: dashboard が骨組みを検証より前に作る (元の欠陥)"
fresh_copy
inject "if [[ \"\$SUBCOMMAND\" == \"dashboard\" ]]; then
  _dashboard_usage_exit() {" "if [[ \"\$SUBCOMMAND\" == \"dashboard\" ]]; then
  mkdir -p \"\$QUEUE_DIR\" \"\$QUEUE_DIR/missions\" \"\$QUEUE_DIR/archive\"
  _dashboard_usage_exit() {"
expect_red "case M" "test_help_prints_usage_and_writes_nothing_on_a_fresh_directory.*dashboard"

echo "== case N: dashboard が -h / --help を usage にしない"
fresh_copy
inject "      -h|--help) echo \"Usage: plan.sh dashboard [--all]\"; exit 0 ;;
" ""
expect_red "case N" "test_help_prints_usage_and_writes_nothing_on_a_fresh_directory.*dashboard"

echo "== case O: dashboard が未知の option を受理する"
fresh_copy
inject "      --all) ;;
      *)
        if [[ \"\$_dashboard_arg\" =~" "      --all) ;;
      *) ;;
      __unreachable__)
        if [[ \"\$_dashboard_arg\" =~"
expect_red "case O" "test_unknown_option_is_rejected_and_nothing_is_written.*dashboard"

echo "== case P: bash 側に新しいサブコマンドを足して usage 行に載せない"
fresh_copy
inject "# ─── dashboard TUI (fzf + gum) ───────────────────────────────────────────────" "if [[ \"\$SUBCOMMAND\" == \"frobnicate\" ]]; then
  mkdir -p \"\$QUEUE_DIR/frobnicate\"
  exit 0
fi

# ─── dashboard TUI (fzf + gum) ───────────────────────────────────────────────"
expect_red "case P" "test_the_tested_subcommands_are_exactly_dispatch_plus_bash_side_branches"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
