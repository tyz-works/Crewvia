#!/usr/bin/env bash
# ci-run-tests-sh.sh — tests/*.sh を glob で全部走らせる（CI の tests-sh-tests job の本体）
#
# scripts/ci-run-script-tests.sh (scripts/test_*.sh 用) と同じ型（t063 / backlog #30
# P3-3）。名指しの一覧を持たない。tests/*.sh に足したファイルは、何もしなくても
# 次の CI から RUN か SKIP のどちらかに入る。走らせないものは
# scripts/ci-tests-sh-excluded.txt（`<path> | <理由>`）だけが決める。
#   - 1 本ずつ走らせ、落ちても残りを止めない（最後に一覧を出して、1 本でも落ちていれば exit 1）
#   - 走らせた件数が 0 のときは PASS にしない（glob が空振りしたら exit 1）
#   - 除外ファイルの行に理由が無い・存在しないファイルを指す・重複している、のいずれかで exit 2
#
# scripts/ci-run-script-tests.sh とコードを共有しない理由（t063 Result 参照）:
# 走らせる glob (`scripts/test_*.sh` 相手は前置詞固定・大半が RUN / `tests/*.sh`
# 相手は前置詞なしの任意名・大半が SKIP)・ファイル名の許容文字（`tests/*.sh` は
# `watchdog-idle-e2e.sh` のようにハイフンを含む）・除外理由の分類がそれぞれ違い、
# 既に 4 並行 PR が依存する ci-run-script-tests.sh 側に抽象化を挟むと、共有関数の
# バグが両方の CI job に同時に波及するリスクの方が重複コードより高いと判断した。
# 「形」（glob + 理由付き allowlist + 構造ガード）は踏襲し、コメント判定・
# ファイル名の許容文字の定義は pytest 側 (tests/test_ci_runs_every_tests_sh.py の
# TEST_FILENAME_PATTERN) と揃える先をここに明記する（t043 P3-1/P3-2 と同じ
# 「3 箇所の定義が食い違う」事故を、コード共有ではなく相互参照コメントで防ぐ）。
#
# usage:
#   scripts/ci-run-tests-sh.sh          走らせる
#   scripts/ci-run-tests-sh.sh --list   走らせずに RUN / SKIP の別を出す（tests/test_ci_runs_every_tests_sh.py が使う）
#
# ローカルで走らせるときは実 checkout を避け、隔離コピーで CREWVIA_* を外して走らせること
# （テストによっては registry に残骸を残す。knowledge/ci-script-tests.md）。
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ALLOWLIST="$ROOT/scripts/ci-tests-sh-excluded.txt"

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
  echo "[ci-tests-sh] ERROR: 除外ファイルが無い: ${ALLOWLIST#"$ROOT"/}" >&2
  exit 2
fi
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  # コメント判定は行を trim した後だけを見る（pytest の parser と同じ規則。
  # scripts/ci-run-script-tests.sh の t043 P3-1 と同じ規則をここでも踏襲する）。
  trimmed=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  case "$trimmed" in ""|"#"*) continue ;; esac
  line="$trimmed"
  case "$line" in
    *"|"*) ;;
    *) echo "[ci-tests-sh] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: '<path> | <理由>' の形ではない（理由が無い）: $line" >&2
       problems=$((problems + 1)); continue ;;
  esac
  path=${line%%|*}
  reason=${line#*|}
  path=$(printf '%s' "$path" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  reason=$(printf '%s' "$reason" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [ -z "$reason" ]; then
    echo "[ci-tests-sh] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 理由が空: $path" >&2
    problems=$((problems + 1)); continue
  fi
  # path の許容文字は tests/test_ci_runs_every_tests_sh.py の
  # TEST_FILENAME_PATTERN (tests/[A-Za-z0-9_-]+\.sh) と同じ字クラスにする
  # （scripts/test_*.sh 側と違い、tests/*.sh はハイフン入りの名前
  # (watchdog-idle-e2e.sh) を実際に含む）。
  if ! [[ "$path" =~ ^tests/[A-Za-z0-9_-]+\.sh$ ]]; then
    echo "[ci-tests-sh] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: tests/*.sh ではない: $path" >&2
    problems=$((problems + 1)); continue
  fi
  if [ ! -f "$ROOT/$path" ]; then
    echo "[ci-tests-sh] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 存在しないファイル: $path" >&2
    problems=$((problems + 1)); continue
  fi
  if printf '%s\n' "$excluded" | grep -qxF -- "$path"; then
    echo "[ci-tests-sh] ERROR: ${ALLOWLIST#"$ROOT"/}:$lineno: 重複: $path" >&2
    problems=$((problems + 1)); continue
  fi
  excluded="${excluded:+$excluded
}$path"
done < "$ALLOWLIST"
if [ "$problems" -ne 0 ]; then
  echo "[ci-tests-sh] 除外ファイルに $problems 件の問題がある" >&2
  exit 2
fi

# --- glob で拾って RUN / SKIP に振り分ける ---
total=0; ran=0; skipped=0; failed=0
failed_list=""
for f in "$ROOT"/tests/*.sh; do
  [ -f "$f" ] || continue
  rel="tests/$(basename "$f")"
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
    echo "[ci-tests-sh] PASS $rel"
  else
    echo "[ci-tests-sh] FAIL $rel (exit $rc)"
    failed=$((failed + 1))
    failed_list="${failed_list:+$failed_list
}  $rel"
  fi
done

echo "[ci-tests-sh] 検査した tests/*.sh: $total 本（実行 $ran / 除外 $skipped / 失敗 $failed）"
if [ "$total" -eq 0 ] || [ "$ran" -eq 0 ]; then
  echo "[ci-tests-sh] ERROR: 走らせたテストが 0 本。glob が空振りしている（PASS にしない）" >&2
  exit 1
fi
[ "$mode" = list ] && exit 0
if [ "$failed" -ne 0 ]; then
  echo "[ci-tests-sh] 失敗したテスト:"
  printf '%s\n' "$failed_list"
  exit 1
fi
exit 0
