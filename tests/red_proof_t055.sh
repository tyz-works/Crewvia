#!/usr/bin/env bash
# t055 (B4 fix / PR#236 Codex findings) — 欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t055.sh
#
#   baseline — いまの木では tests/test_task_deliverable.py が緑
#   A  lint: can_produce_deliverable の値の正規表現が空白を含む値で欄ごと読み飛ばす → 赤
#   B  lint: skills が空リスト・欠落の task を deliverable 検査から丸ごと飛ばす      → 赤
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
# pytest は隔離した queue・registry (Sandbox) で動き、本物の herdr / tmux / 本番 queue には届かない。
# PYTHONDONTWRITEBYTECODE=1 で __pycache__ を作らない (古い .pyc が注入を隠さないように)。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t055.XXXXXX")"
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

# inject <file> <old> <new> — 複製した <file> の old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    FILE="$1" OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"count={s.count(old)}", file=sys.stderr)
    sys.exit(1)
p.write_text(s.replace(old, new))
PY
}

# 呼び出し元の AGENT_NAME / SKILLS / CREWVIA_* を引き継がない (テストは自前の env を組む)
run_py() {  # run_py <pytest の引数...>
    ( cd "$TREE" && env -i PATH="$PATH" HOME="$WORK" PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}" \
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "$@" 2>&1 )
}
TESTS=(tests/test_task_deliverable.py)

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local name="$1" frag="$2"
    local out; out="$(run_py "${TESTS[@]}")"
    if echo "$out" | grep -q "FAILED .*$frag"; then ok "$name → 赤 ($frag)"
    else ng "$name → 赤にならなかった ($frag)"; echo "$out" | tail -8; fi
}

fresh_copy
echo "== baseline"
out="$(run_py "${TESTS[@]}")"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ failed"; then ok "baseline は緑"
else ng "baseline が緑でない"; echo "$out" | tail -8; fi

echo "== case A: can_produce_deliverable の正規表現が空白を含む値で欄を読み飛ばす"
fresh_copy
inject scripts/lint_plan.py \
"_CAPABILITY_LINE = re.compile(r'^    can_produce_deliverable:\s*(.*)\$')" \
"_CAPABILITY_LINE = re.compile(r'^    can_produce_deliverable:\s*([^#\s]*)\s*(?:#.*)?\$')"
expect_red "case A" "test_a_malformed_capability_is_a_fail_not_silently_true_or_false"

echo "== case B: skills が空リスト・欠落の task を deliverable 検査から丸ごと飛ばす"
fresh_copy
inject scripts/lint_plan.py \
"        skills = meta.get('skills')
        if not isinstance(skills, list) or not skills:
            # skills が無い・空リストの task は producer skill を 1 つも持てない。
            # check_frontmatter は \`skills: []\` を有効な frontmatter として通すので、
            # ここで前提を預けず自分で閉じる (欠落・空リストのどちらも FAIL)。
            results.append(('FAIL', 'deliverable',
                            f\"{prefix}: deliverable '{declared}' を宣言していますが 'skills' が空です \"
                            f\"(skills={skills!r}) — 成果物を作れる skill を足すか、deliverable を none にする\"))
            continue" \
"        skills = meta.get('skills')
        if not isinstance(skills, list) or not skills:
            continue  # skills の型・欠落は frontmatter 検査の担当"
expect_red "case B" "test_an_empty_skills_list_fails_because_no_skill_can_produce_it"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
