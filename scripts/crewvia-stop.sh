#!/usr/bin/env bash
# crewvia-stop — crewvia を安全に停止する
#
# Usage:
#   scripts/crewvia-stop.sh [options]
#   ./crewvia-stop [options]         # ← repo root wrapper 経由
#
# Options:
#   --force, -f        active mission がある場合も強制停止する
#   --full             herdr server 自体も停止する (全 pane が死ぬ)
#   --keep-director    *-director tab を残す (herdr pane 内実行時は自動で付く)
#   --dry-run, -n      実行せず何をするか表示のみ
#   -h, --help         このヘルプを表示
#
# 停止順序:
#   1) worker tabs (*-worker)
#   2) dispatcher, watchdog
#   3) *-director tab (--keep-director で skip)
#   4) --full 指定時: herdr server stop

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE=0
FULL=0
KEEP_DIRECTOR=0
DRY_RUN=0

while (($#)); do
  case "$1" in
    --force|-f) FORCE=1 ;;
    --full) FULL=1 ;;
    --keep-director) KEEP_DIRECTOR=1 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    -h|--help)
      # ヘッダーコメントの Usage 部分を抜粋表示
      sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $1 (see --help)" >&2
      exit 2
      ;;
  esac
  shift
done

# crewvia repo かの sanity check
if [[ ! -f "${REPO_ROOT}/scripts/lib_mux.py" ]] || [[ ! -f "${REPO_ROOT}/config/crewvia.yaml" ]]; then
  echo "ERROR: ${REPO_ROOT} は crewvia repo ではないようです" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 自動 --keep-director: HERDR_TAB_ID セット時は herdr pane 内で実行中
# → Director tab 内で自分を kill すると script が SIGKILL される
# ---------------------------------------------------------------------------
if [[ -n "${HERDR_TAB_ID:-}" ]] && [[ $KEEP_DIRECTOR -eq 0 ]]; then
  echo "→ herdr pane 内から実行を検知 (HERDR_TAB_ID=${HERDR_TAB_ID})"
  echo "  自動で --keep-director を有効化 (自身の tab を kill しない)"
  echo "  完全停止したい場合: 別ターミナルから実行 or --full を使う"
  echo
  KEEP_DIRECTOR=1
fi

# ---------------------------------------------------------------------------
# active mission check (plan.sh status を source of truth に使う。
# queue/missions/ ディレクトリ数は archive 前の残骸や test 用の dir を含む可能性
# があり不正確)
# ---------------------------------------------------------------------------
plan_status_output=$("${REPO_ROOT}/scripts/plan.sh" status 2>&1 | grep -v '^\[plan.sh\] WARNING' || true)
active_count=0
if [[ -n "$plan_status_output" ]] && ! echo "$plan_status_output" | grep -q "^No active missions"; then
  # "Active missions (N):" 行から N を取得
  active_count=$(echo "$plan_status_output" | sed -n 's/^Active missions (\([0-9]*\)):.*/\1/p' | head -1)
  active_count=${active_count:-0}
fi

if [[ $active_count -gt 0 ]]; then
  echo "⚠ Active mission ($active_count 件) 存在:"
  echo "$plan_status_output" | head -20
  echo
  if [[ $FORCE -eq 0 ]]; then
    echo "ERROR: active mission がある状態での停止は --force が必要です" >&2
    exit 1
  fi
  echo "→ --force により mission 状態を無視して継続"
  echo
fi

# ---------------------------------------------------------------------------
# 現状のタブ一覧
# ---------------------------------------------------------------------------
mapfile -t TABS < <(python3 "${REPO_ROOT}/scripts/lib_mux.py" list 2>/dev/null || true)

if [[ ${#TABS[@]} -eq 0 ]]; then
  echo "→ 稼働中の tab なし"
  if [[ $FULL -eq 0 ]]; then
    echo "✓ 既に停止済み"
    exit 0
  fi
else
  echo "→ 現在の tabs (${#TABS[@]} 件):"
  printf '   %s\n' "${TABS[@]}"
  echo
fi

# tab を kill するヘルパー (dry-run 対応)
kill_tab() {
  local name="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] kill: $name"
  else
    echo "  kill: $name"
    python3 "${REPO_ROOT}/scripts/lib_mux.py" kill "$name" 2>&1 | grep -v "pane not found" || true
  fi
}

# ---------------------------------------------------------------------------
# 1) worker tabs
# ---------------------------------------------------------------------------
echo "→ worker tabs を停止..."
worker_count=0
for t in "${TABS[@]}"; do
  if [[ "$t" == *-worker ]]; then
    kill_tab "$t"
    worker_count=$((worker_count + 1))
  fi
done
[[ $worker_count -eq 0 ]] && echo "  (なし)"

# ---------------------------------------------------------------------------
# 2) dispatcher / watchdog
# ---------------------------------------------------------------------------
echo "→ dispatcher / watchdog を停止..."
infra_count=0
for t in "${TABS[@]}"; do
  case "$t" in
    dispatcher|watchdog)
      kill_tab "$t"
      infra_count=$((infra_count + 1))
      ;;
  esac
done
[[ $infra_count -eq 0 ]] && echo "  (なし)"

# ---------------------------------------------------------------------------
# 3) director tab
# ---------------------------------------------------------------------------
if [[ $KEEP_DIRECTOR -eq 0 ]]; then
  echo "→ director tab を停止..."
  director_count=0
  for t in "${TABS[@]}"; do
    if [[ "$t" == *-director ]]; then
      kill_tab "$t"
      director_count=$((director_count + 1))
    fi
  done
  [[ $director_count -eq 0 ]] && echo "  (なし)"
else
  echo "→ director tab はスキップ (--keep-director)"
fi

# ---------------------------------------------------------------------------
# 4) --full: herdr server 自体を停止
# ---------------------------------------------------------------------------
if [[ $FULL -eq 1 ]]; then
  echo "→ herdr server を停止..."
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] herdr server stop"
  else
    if command -v herdr >/dev/null 2>&1; then
      herdr server stop 2>&1 | head -3 || echo "  (herdr server stop failed — 既に停止済みかも)"
    else
      echo "  (herdr binary が見つかりません、スキップ)"
    fi
  fi
fi

echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "✓ dry-run 完了 (実際の停止処理は行っていません)"
else
  echo "✓ crewvia stop 完了"
fi
