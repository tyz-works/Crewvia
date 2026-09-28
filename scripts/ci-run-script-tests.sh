#!/usr/bin/env bash
# ci-run-script-tests.sh — scripts/test_*.sh を glob で全部走らせる（CI の script-tests job の本体）
#
# 名指しの一覧を持たない。scripts/test_*.sh に足したテストは、何もしなくても次の CI から走る。
# 走らせないものは scripts/ci-script-tests-excluded.txt（`<path> | <理由>`）だけが決める。
#   - 1 本ずつ走らせ、落ちても残りを止めない（最後に一覧を出して、1 本でも落ちていれば exit 1）
#   - 走らせた件数が 0 のときは PASS にしない（glob が空振りしたら exit 1）
#   - 除外ファイルの行に理由が無い・存在しないファイルを指す・重複している、のいずれかで exit 2
#
# usage:
#   scripts/ci-run-script-tests.sh          走らせる
#   scripts/ci-run-script-tests.sh --list   走らせずに RUN / SKIP の別を出す（tests/test_ci_runs_every_script_test.py が使う）
#
# ローカルで走らせるときは実 checkout を避け、隔離コピーで CREWVIA_* を外して走らせること
# （テストによっては registry に残骸を残す。knowledge/ci-script-tests.md）。
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ALLOWLIST="$ROOT/scripts/ci-script-tests-excluded.txt"

mode=run
case "${1:-}" in
  "") ;;
  --list) mode=list ;;
  *) echo "usage: $0 [--list]" >&2; exit 2 ;;
esac

# --- 除外ファイルを読む（ここで形式を検査する。黙って読み飛ばさない） ---
excluded=""   # 改行区切りの path 一覧
problems=0
if [ ! -f "$ALLOWLIST" ]; then
  echo "[ci-script-tests] ERROR: 除外ファイルが無い: ${ALLOWLIST#"$ROOT"/}" >&2
  exit 2
fi
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  # コメント判定は行を trim した後だけを見る（pytest の parser と同じ規則。t043 P3-1:
  # 以前は [[:space:]]*"#"* が「先頭が空白1個 + 行のどこかに#」にマッチし、
  # 引用符付き path の途中に # を含む有効な行まで誤ってコメット扱いしていた）。
  trimmed=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case "$trimmed" in ""|"#"*) continue ;; esac
  line="$trimmed"
  case "$line" in
    *"|"*) ;;
    *) echo "[ci-script-tests] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: '<path> | <理由>' の形ではない（理由が無い）: $line" >&2
       problems=$((problems + 1)); continue ;;
  esac
  path=${line%%|*}
  reason=${line#*|}
  path=$(printf '%s' "$path" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  reason=$(printf '%s' "$reason" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [ -z "$reason" ]; then
    echo "[ci-script-tests] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 理由が空: $path" >&2
    problems=$((problems + 1)); continue
  fi
  # path の許容文字は tests/test_ci_runs_every_script_test.py の
  # TEST_FILENAME_PATTERN (scripts/test_[A-Za-z0-9_]+\.sh) と同じ字クラス
  # にする（t043 P3-2: 以前は glob `scripts/test_*.sh` で `-` 等も受理し、
  # pytest 側だけが赤くなる食い違いがあった）。
  if ! [[ "$path" =~ ^scripts/test_[A-Za-z0-9_]+\.sh$ ]]; then
    echo "[ci-script-tests] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: scripts/test_*.sh ではない: $path" >&2
    problems=$((problems + 1)); continue
  fi
  if [ ! -f "$ROOT/$path" ]; then
    echo "[ci-script-tests] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 存在しないファイル: $path" >&2
    problems=$((problems + 1)); continue
  fi
  if printf '%s\n' "$excluded" | grep -qxF -- "$path"; then
    echo "[ci-script-tests] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 重複: $path" >&2
    problems=$((problems + 1)); continue
  fi
  excluded="${excluded:+$excluded
}$path"
done < "$ALLOWLIST"
if [ "$problems" -ne 0 ]; then
  echo "[ci-script-tests] 除外ファイルに $problems 件の問題がある" >&2
  exit 2
fi

# --- glob で拾って RUN / SKIP に振り分ける ---
total=0; ran=0; skipped=0; failed=0
failed_list=""
for f in "$ROOT"/scripts/test_*.sh; do
  [ -f "$f" ] || continue
  rel="scripts/$(basename "$f")"
  total=$((total + 1))
  if [ -n "$excluded" ] && printf '%s\n' "$excluded" | grep -qxF -- "$rel"; then
    skipped=$((skipped + 1))
    [ "$mode" = list ] && echo "SKIP $rel"
    continue
  fi
  if [ "$mode" = list ]; then
    ran=$((ran + 1)); echo "RUN $rel"; continue
  fi
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::group::$rel"
  ( cd "$ROOT" && bash "$rel" )
  rc=$?
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::endgroup::"
  ran=$((ran + 1))
  if [ "$rc" -eq 0 ]; then
    echo "[ci-script-tests] PASS $rel"
  else
    echo "[ci-script-tests] FAIL $rel (exit $rc)"
    failed=$((failed + 1))
    failed_list="${failed_list:+$failed_list
}  $rel"
  fi
done

echo "[ci-script-tests] 検査した scripts/test_*.sh: $total 本（実行 $ran / 除外 $skipped / 失敗 $failed）"
if [ "$total" -eq 0 ] || [ "$ran" -eq 0 ]; then
  echo "[ci-script-tests] ERROR: 走らせたテストが 0 本。glob が空振りしている（PASS にしない）" >&2
  exit 1
fi
[ "$mode" = list ] && exit 0
if [ "$failed" -ne 0 ]; then
  echo "[ci-script-tests] 失敗したテスト:"
  printf '%s\n' "$failed_list"
  exit 1
fi
exit 0
