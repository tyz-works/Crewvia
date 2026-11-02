#!/usr/bin/env bash
# review skill の card に既定で timeout.max を付与する変更 (t002 / 20261102-review-ci-timeout) が、
# 欠陥を戻すと tests/test_timeout_skill_defaults.py が赤くなることの実証。
#
# 使い方:  bash tests/red_proof_t002_review_ci_timeout.sh
#
#   baseline — いまの木では全テストが緑
#   case A   — plan.sh add の既定付与ロジックを削除 (скill 一致で timeout.max を書く分岐を消す)
#              → review skill の card に既定が付かなくなる → 1, 4 系のテストが赤
#   case B   — 複数 skill の解決を「最初に見つかった値」に戻す (最も緩い方を採らない)
#              → test_multiple_matching_skills_pick_the_more_lenient_value が赤
#   case C   — plan.sh update から --max-timeout / --idle-timeout を削除
#              → update 系のテストが collect で失敗 (未知の option)
#
# 隔離: 使い捨ての複製で欠陥を注入する。本番の worktree のファイルには触らない。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t002.XXXXXX")"
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

# inject <file(TREE 相対)> <old> <new> — old を new に置換 (ちょうど 1 か所。無ければ FATAL)
inject() {
    OLD="$2" NEW="$3" python3 - "$TREE/$1" <<'PY' || { echo "FATAL: 注入点が見つからない ($1)"; exit 2; }
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
        PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_timeout_skill_defaults.py -p no:cacheprovider 2>&1 )
}

expect_red() {  # expect_red <case名> <赤になるはずのテスト名の断片>
    local out; out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*$2"; then ok "$1 → 赤 ($2)"
    else ng "$1 → 赤にならなかった ($2)"; echo "$out" | tail -12; fi
}

fresh_copy
echo "== baseline"
out="$(run_suite)"
if echo "$out" | grep -q " passed" && ! echo "$out" | grep -qE "[0-9]+ (failed|error)"; then
    ok "baseline は緑 ($(echo "$out" | tail -1))"
else
    ng "baseline が緑でない"; echo "$out" | tail -12
fi

echo "== case A: 既定付与ロジックを削除 (review skill に timeout.max が付かなくなる)"
fresh_copy
if inject scripts/plan.sh \
    "    else:
        # --max-timeout が明示されなければ skill 別既定で埋める (t002 / 20261102-review-ci-timeout)。
        # 明示値がある card には絶対に触らない (上の分岐が先に timeout['max'] を確定させているので
        # ここには来ない)。
        default_max = _default_timeout_max_for_skills(skills)
        if default_max is not None:
            timeout['max'] = default_max" \
    "    else:
        pass  # red-proof: 既定付与を取り除いた"; then
    out="$(run_suite)"
    if echo "$out" | grep -q "FAILED .*test_review_skill_gets_default_timeout_max" \
       && echo "$out" | grep -q "FAILED .*test_review_mixed_with_other_skills_still_gets_default" \
       && echo "$out" | grep -q "FAILED .*test_idle_timeout_flag_is_independent_of_the_default" \
       && echo "$out" | grep -q "FAILED .*test_multiple_matching_skills_pick_the_more_lenient_value" \
       && echo "$out" | grep -q "FAILED .*test_update_idle_timeout_merges_with_existing_max"; then
        ok "case A → add 系・update マージ系のテストが赤 (既定付与が無いと review の card に timeout が付かない)"
    else
        ng "case A → 期待したテストが赤にならなかった"; echo "$out" | tail -20
    fi
else
    ng "case A → 注入点が見つからない (コードが動いた? このスクリプトを直すこと)"
fi

echo "== case B: 複数 skill の解決を「最初に見つかった値」に戻す"
fresh_copy
if inject scripts/plan.sh \
    "    candidates = [v for s, v in mapping.items() if s in skills
                  and isinstance(v, int) and not isinstance(v, bool) and v > 0]
    return max(candidates) if candidates else None" \
    "    for s in skills:
        v = mapping.get(s)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v  # red-proof: 最初に見つかった値 (最も緩い方を採らない)
    return None"; then
    expect_red "case B" "test_multiple_matching_skills_pick_the_more_lenient_value"
else
    ng "case B → 注入点が見つからない"
fi

echo "== case C: plan.sh update から --max-timeout / --idle-timeout を削除"
fresh_copy
if inject scripts/plan.sh \
    "        '--close-execution': 'bool',
        '--idle-timeout': 'value',
        '--max-timeout': 'value',
    })" \
    "        '--close-execution': 'bool',
    })"; then
    out="$(run_suite)"
    if echo "$out" | grep -qE "[0-9]+ (failed|error)"; then
        ok "case C → 赤 (unknown option で update 系テストが落ちる)"
    else
        ng "case C → 赤にならなかった"; echo "$out" | tail -12
    fi
else
    ng "case C → 注入点が見つからない"
fi

echo
echo "合計: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
