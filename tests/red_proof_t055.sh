#!/usr/bin/env bash
# t055 (B4 fix / PR#236 Codex findings) — 欠陥を戻すと赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t055.sh
#
#   baseline — いまの木では tests/test_task_deliverable.py が緑
#   A  (廃止 — t059 で `_CAPABILITY_LINE` ごと削除。同じ欠陥は case C が包含して検出する)
#   B  lint: skills が空リスト・欠落の task を deliverable 検査から丸ごと飛ばす      → 赤
#   C  lint: 本物の YAML パーサを丸ごと旧来の手書き行パーサへ戻す (t059 / PR#236 2巡目)
#      — コメント付きヘッダで宣言が隠れる・前の skill に誤って付く・フロースタイルが
#        読めない、のいずれも再現する (case A の欠陥もこの中に含まれる)             → 赤
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

# inject_span <file> <old_content_file> <new_content_file> — old/new をファイル経由で渡す版。
# バッククォート・$ を多く含む大きなブロックを bash の引用符地獄なしで置換するために使う。
inject_span() {
    FILE="$1" OLDFILE="$2" NEWFILE="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = pathlib.Path(os.environ["OLDFILE"]).read_text()
new = pathlib.Path(os.environ["NEWFILE"]).read_text()
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

echo "== case A: 廃止 (旧 _CAPABILITY_LINE は t059 で削除済み。case C が同じ欠陥を包含して検出)"

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

echo "== case C: 本物の YAML パーサを丸ごと旧来の手書き行パーサへ戻す (t059 / PR#236 2巡目)"
fresh_copy
sed -n '/^#: `can_produce_deliverable` は厳密に/,/^    return caps, None$/p' \
    "$TREE/scripts/lint_plan.py" > "$WORK/new_block.txt"
if [ ! -s "$WORK/new_block.txt" ]; then
    echo "FATAL: 置換対象の新実装ブロックが見つからない (マーカーがずれた?)"; exit 2
fi
cat > "$WORK/old_block.txt" <<'BLOCK'
#: 値部分は行末までまるごと取る (`[^#\s]*` は空白を含む値 — `not a boolean` や
#: `[false, true]` — で行全体にマッチせず、欄が「無い」ものとして読み飛ばされていた)。
#: インラインコメントは値を取り出した後に文字列として切り落とす。
_CAPABILITY_LINE = re.compile(r'^    can_produce_deliverable:\s*(.*)$')


def _load_deliverable_capabilities(skill_permissions_path: str) -> tuple[dict, Optional[str]]:
    """`{skill: True | False | <不正な値の文字列>}` と、読めなかった理由 (読めたら None)。

    欄の無いスキルは辞書に載せない (= 呼び出し側は「作れる」と読む)。値は `true` / `false` の
    どちらかだけを受け入れ、それ以外は **文字列のまま** 返す (truthiness で False に潰さない —
    `flase` と書き間違えたスキルを「作れる」にも「作れない」にも黙って倒さないため)。
    空白を含む値 (文字列・リスト表記など) や空値も、欄自体は「ある」ものとして拾い、
    不正な値の文字列として返す (欄の有無と値の妥当性を別に扱う)。
    """
    try:
        with open(skill_permissions_path, encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {}, f"{skill_permissions_path}: {type(e).__name__}: {e}"
    caps: dict = {}
    in_skills = False
    current: Optional[str] = None
    for line in content.splitlines():
        if re.match(r'^skills:\s*$', line):
            in_skills = True
            continue
        if not in_skills:
            continue
        m = re.match(r'^  ([a-zA-Z_][a-zA-Z0-9_-]*):\s*$', line)
        if m:
            current = m.group(1)
            continue
        if line and not line.startswith(' ') and not line.startswith('#'):
            in_skills = False
            current = None
            continue
        cm = _CAPABILITY_LINE.match(line)
        if cm and current is not None:
            raw = re.sub(r'\s*#.*$', '', cm.group(1)).strip()
            caps[current] = {'true': True, 'false': False}.get(raw, raw)
    return caps, None
BLOCK
inject_span scripts/lint_plan.py "$WORK/new_block.txt" "$WORK/old_block.txt"
expect_red "case C-1 (コメント付き skill ヘッダで宣言が隠れる)" "test_a_commented_skill_header_still_yields_its_own_declaration"
expect_red "case C-2 (コメント付き親ヘッダで skills 全体が読まれない)" "test_a_commented_parent_skills_header_still_starts_the_skills_section"
expect_red "case C-3 (capability が前の skill に誤って付く)" "test_a_capability_after_a_commented_header_is_not_misassigned_to_the_previous_skill"
expect_red "case C-4 (フロースタイルが読めない)" "test_a_flow_style_value_is_read_as_a_real_boolean"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
