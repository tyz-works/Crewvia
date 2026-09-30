#!/usr/bin/env bash
# 赤の実証 (t035): 検出器が「コメント・引用符の中の <<X を heredoc の開始と誤認する」旧い形に戻ると、
# 修正で足したテストが赤になること。使い捨ての複製 (mktemp) の中だけで欠陥を注入する — 本物のファイルは変えない。
# (複製は終了時に `.done` へ移すだけで消さない。tests/red_proof_s5_lib_writers.sh と同じ作法)
#
#   bash tests/red_proof_queue_write_scan_heredoc.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'mv "$WORK" "$WORK.done" 2>/dev/null || true' EXIT

mkdir -p "$WORK/tests"
cp -R "$REPO/scripts" "$REPO/hooks" "$WORK/"
cp "$REPO/crewvia" "$REPO/crewvia-stop" "$WORK/"
cp "$REPO/tests/queue_write_scan.py" "$REPO/tests/test_queue_writes_go_through_the_store.py" "$WORK/tests/"
find "$WORK" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true

run() {
  (cd "$WORK" && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
    tests/test_queue_writes_go_through_the_store.py 2>&1)
}

echo "== 1. 修正後: 緑であること"
out="$(run)" || { echo "$out" | tail -15; echo "FAIL: 修正後が緑でない"; exit 1; }
echo "$out" | tail -1

echo "== 2. 欠陥注入: heredoc の開始を、引用符・コメントを見ずに拾う (旧検出器の形)"
python3 - "$WORK/tests/queue_write_scan.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
needle = "for pos, term in _lex_line(line, stack)[2]:"
assert s.count(needle) == 1, "注入点が無い: red proof が失効している"
s = s.replace(needle, "for pos, term in [(m.start(), m.group(2)) for m in _HEREDOC_RE.finditer(re.sub(r'<<<', '   ', line))]:")
open(p, 'w', encoding='utf-8').write(s)
PY
find "$WORK" -name '__pycache__' -prune -exec rm -r {} + 2>/dev/null || true
set +e
out="$(run)"
rc=$?
set -e
echo "$out" | grep -E "^(FAILED|[0-9]+ (passed|failed))" | sed 's/ - .*//' | head -20
[ "$rc" -ne 0 ] || { echo "FAIL: 欠陥を入れても緑のまま (red proof が効いていない)"; exit 1; }
for must in test_no_target_has_an_unclosed_heredoc_or_quote \
            test_positive_control_a_write_appended_to_the_previously_blind_files_is_detected \
            test_negative_control_heredoc_lookalike_does_not_blind_the_rest; do
  echo "$out" | grep -q "FAILED.*$must" || { echo "FAIL: $must が赤になっていない"; exit 1; }
done
echo "OK: 欠陥を入れると 3 種のテストが赤になる"
