#!/usr/bin/env bash
# t053 — tests/test_observation_authority_does_not_fail_open.py が
# 「新しい未分類の観測呼び出し」を実際に赤にすることの実証。
#
# このガードは純粋な AST 静的検査 (プロセスを kill しない・/proc を走査しない) なので、
# 他の red_proof_*.sh のような PID 名前空間隔離は不要 — 注入は **使い捨てのコピー**
# (scripts/worktree_gc.py を $WORK にコピーしたもの) にだけ行い、本番の worktree の
# ファイルには一切触れない。
#
#   baseline — 現状の木で対象テストが緑
#   A        — 使い捨てコピーに「未分類の」新しい観測呼び出し (stat 失敗 → uid 0 を返す
#              fail-open な作り) を注入し、AUTHORITY_MODULES をそのコピーに差し替えて
#              実際の test_every_risky_call_is_classified() を呼ぶと、注入した箇所だけが
#              落ちること (他の既存の箇所は緑のまま) を確かめる
#   B        — 同じ状態で test_the_allowlist_has_no_dead_entries() は緑のまま
#              (「死んだ行」の話ではないので落ちてはいけない — 打ち切り例外の相殺が
#              無いことの確認)
#   C        — 本番の木 (real AUTHORITY_MODULES) はここまで一切変更していないので、
#              対象テストファイルをそのまま (invoke) 走らせても緑のまま
#              (= 「注入を戻すと緑に戻る」の確認。実際には最初から本番へは何も注入して
#              いない — 使い捨てコピーだけを差し替えた)
#
# 使い方:  bash tests/red_proof_t053.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/red-proof-t053.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

echo "== baseline: 本番の木でガードが緑 =="
if python3 -m pytest "$REPO_ROOT/tests/test_observation_authority_does_not_fail_open.py" -q \
    >"$WORK/baseline.log" 2>&1; then
  ok "baseline green"
else
  ng "baseline should be green (before any injection)"
  cat "$WORK/baseline.log"
fi

echo "== A/B: 使い捨てコピーに未分類の観測呼び出しを注入し、その 1 件だけが赤くなる =="
cp "$REPO_ROOT/scripts/worktree_gc.py" "$WORK/worktree_gc.py"
cat >>"$WORK/worktree_gc.py" <<'PYEOF'


def _t053_injected_fail_open_probe(pid):
    """red_proof_t053.sh が注入する、まだ OBSERVATION_SITES に無い観測呼び出し。
    実際の欠陥と同じ形: stat が読めなければ「対象は居ない (uid 0 扱い)」に潰す。"""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return 0
PYEOF

python3 - "$REPO_ROOT" "$WORK/worktree_gc.py" <<'PYEOF'
import importlib.util
import pathlib
import sys

repo_root, injected = sys.argv[1], sys.argv[2]
test_path = pathlib.Path(repo_root) / "tests" / "test_observation_authority_does_not_fail_open.py"
spec = importlib.util.spec_from_file_location("t053_guard", str(test_path))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 実物の AUTHORITY_MODULES に、注入したコピーを追加する (置き換えると他の 7 ファイルが
# dead-entries 扱いになり B の確認が意味を失う)。
mod.AUTHORITY_MODULES = mod.AUTHORITY_MODULES + [pathlib.Path(injected)]
sites = mod._all_sites()

injected_sites = [s for s in sites if s[1] == "_t053_injected_fail_open_probe"]
assert injected_sites, f"注入した関数が _risky_calls() に見つからない: {sites}"
assert len(injected_sites) == 1, injected_sites
injected_site = injected_sites[0]

# --- A: 実際の test_every_risky_call_is_classified() と同じ assert を、注入した箇所に対して呼ぶ ---
try:
    mod.test_every_risky_call_is_classified(injected_site)
except AssertionError:
    print("A: RED CONFIRMED —", injected_site)
else:
    raise SystemExit("A-FAIL: injected site was not caught (should have raised AssertionError)")

# 既存の (本物の) 箇所は、注入とは無関係にそのまま緑であること (相殺していないことの確認)。
real_sites = [s for s in sites if s != injected_site]
assert real_sites, "worktree_gc.py の実在サイトが 1 件も見つからない (検出器が壊れている)"
for s in real_sites:
    mod.test_every_risky_call_is_classified(s)  # raise しなければ OK
print(f"A: 既存の {len(real_sites)} 件は注入と無関係に緑のまま")

# --- B: test_the_allowlist_has_no_dead_entries は「死んだ行」の話であって、
#         今回の注入 (allowlist に無い新規呼び出し) では落ちてはいけない ---
mod.test_the_allowlist_has_no_dead_entries()
print("B: RED CONFIRMED していない = 期待通り緑 (dead-entry ガードは別の懸念)")
PYEOF
if [ $? -eq 0 ]; then
  ok "A: 未分類の観測呼び出し 1 件だけが test_every_risky_call_is_classified を落とす"
  ok "B: test_the_allowlist_has_no_dead_entries は無関係に緑のまま"
else
  ng "A/B: injection did not reproduce the expected red/green split"
fi

echo "== C: 本番の木には何も注入していないので、対象テストは引き続き緑 (revert 相当) =="
if python3 -m pytest "$REPO_ROOT/tests/test_observation_authority_does_not_fail_open.py" -q \
    >"$WORK/after.log" 2>&1; then
  ok "C: production tree still green (nothing was injected into it)"
else
  ng "C: production tree unexpectedly red after the scratch-copy injection"
  cat "$WORK/after.log"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
