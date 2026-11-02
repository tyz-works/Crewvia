#!/usr/bin/env bash
# 赤の実証 (t002, mission 20261102-pr-task-line): hooks/pre-tool-use.sh の
# "PR task-line guard" を削ると tests/test_pr_task_line_guard.py が赤くなること。
# 使い捨ての複製 (mktemp) の中だけで欠陥を注入する — 本物のファイルは変えない。
#
#   bash tests/red_proof_pr_task_line_guard.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'mv "$WORK" "$WORK.done" 2>/dev/null || true' EXIT

mkdir -p "$WORK/tests"
cp -R "$REPO/hooks" "$WORK/"
cp "$REPO/tests/test_pr_task_line_guard.py" "$WORK/tests/"
find "$WORK" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true

run() {
  (cd "$WORK" && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
    tests/test_pr_task_line_guard.py 2>&1)
}

echo "== 1. 修正後: 緑であること"
out="$(run)" || { echo "$out" | tail -20; echo "FAIL: 修正後が緑でない"; exit 1; }
echo "$out" | tail -1

echo "== 2. 欠陥注入: PR task-line guard のブロックを丸ごと削る"
python3 - "$WORK/hooks/pre-tool-use.sh" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
start_marker = "# --- PR task-line guard"
end_marker = "# --- Plan-review write scope guard"
start = s.index(start_marker)
end = s.index(end_marker)
assert start < end, "注入点が無い: red proof が失効している"
s = s[:start] + s[end:]
open(p, 'w', encoding='utf-8').write(s)
PY
find "$WORK" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true
set +e
out="$(run)"
rc=$?
set -e

echo "== 3. 欠陥注入後: 狙ったテストが赤であること"
if [ "$rc" -eq 0 ]; then
  echo "$out" | tail -20
  echo "FAIL: 欠陥を入れても緑のまま (red proof が効いていない)"
  exit 1
fi
for t in test_body_missing_task_line_is_denied test_denial_reason_names_the_expected_line test_gh_pr_create_after_unrelated_command_is_still_checked; do
  if ! echo "$out" | grep -q "FAILED tests/test_pr_task_line_guard.py::${t}"; then
    echo "$out" | tail -30
    echo "FAIL: ${t} が赤くなっていない"
    exit 1
  fi
done
echo "$out" | tail -5
echo "OK: 欠陥注入で期待どおり赤くなった"
