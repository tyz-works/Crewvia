#!/usr/bin/env bash
# scripts/observe-notify.sh — 通知到達統計集計ツール
#
# logs/dispatcher/dispatcher-YYYYMMDD.log と /tmp/dispatcher-notify-cache.json から
# 通知到達統計を集計する。次回以降の観測 mission でも再利用できる汎用ツール。
#
# 集計項目:
#   ①通知送信件数          実際に送信された通知 (→ [target] 行の数)
#   ②notify cache ヒット件数  キャッシュエントリ数 (suppress済み / アクティブ)
#   ③Director 実受信確認件数  Director が受信した通知 (→ [*director*] 行の数)
#
# 使用法:
#   observe-notify.sh [OPTIONS]
#
# オプション:
#   --date YYYYMMDD       集計対象日 (デフォルト: 今日)
#   --last N              最新 N 件の通知ラインのみ集計
#   --json                JSON 形式で出力
#   --notify-cache FILE   notify cache のパス (デフォルト: /tmp/dispatcher-notify-cache.json)
#   --notify-ttl SECONDS  キャッシュ TTL 秒数 (デフォルト: 300)
#   -h, --help            このヘルプを表示

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# デフォルト設定
DATE_ARG=""
LAST_N=""
OUTPUT_JSON=false
NOTIFY_CACHE="/tmp/dispatcher-notify-cache.json"
NOTIFY_TTL=300

# ---------------------------------------------------------------------------
# ヘルプ
# ---------------------------------------------------------------------------
usage() {
  cat <<'EOF'
使用法: observe-notify.sh [OPTIONS]

logs/dispatcher/dispatcher-YYYYMMDD.log と /tmp/dispatcher-notify-cache.json から
Dispatcher の通知到達統計を集計する。

オプション:
  --date YYYYMMDD       集計対象日 (デフォルト: 今日)
  --last N              最新 N 件の通知ラインのみ集計
  --json                JSON 形式で出力
  --notify-cache FILE   notify cache のパス (デフォルト: /tmp/dispatcher-notify-cache.json)
  --notify-ttl SECONDS  キャッシュ TTL 秒数 (デフォルト: 300)
  -h, --help            このヘルプを表示

集計項目:
  ①通知送信件数         : ログ内の「→ [target]」行の総数
  ②notify cache ヒット  : cache エントリ数 (active=TTL内で suppress中, total=全履歴)
  ③Director 実受信確認  : ログ内の「→ [*director*]」行の数

注記:
  ②の suppress 件数はキャッシュファイルの状態から推定します。
  suppress 発生時にログが出ないため、ログのみでは計測できません。
  正確な suppress カウントが必要な場合は dispatcher.py の should_notify() に
  log 呼び出しを追加してください。

例:
  observe-notify.sh                     # 今日のログを集計
  observe-notify.sh --date 20260905     # 指定日のログを集計
  observe-notify.sh --last 50           # 最新50件の通知ラインのみ集計
  observe-notify.sh --json              # JSON 形式で出力
  observe-notify.sh --date 20260905 --json | jq .stats
EOF
}

# ---------------------------------------------------------------------------
# 引数パース
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)
      [[ $# -ge 2 ]] || { echo "ERROR: --date には引数が必要です" >&2; exit 1; }
      DATE_ARG="$2"; shift 2 ;;
    --last)
      [[ $# -ge 2 ]] || { echo "ERROR: --last には引数が必要です" >&2; exit 1; }
      LAST_N="$2"; shift 2 ;;
    --json)
      OUTPUT_JSON=true; shift ;;
    --notify-cache)
      [[ $# -ge 2 ]] || { echo "ERROR: --notify-cache には引数が必要です" >&2; exit 1; }
      NOTIFY_CACHE="$2"; shift 2 ;;
    --notify-ttl)
      [[ $# -ge 2 ]] || { echo "ERROR: --notify-ttl には引数が必要です" >&2; exit 1; }
      NOTIFY_TTL="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "ERROR: 不明なオプション: $1" >&2
      usage >&2
      exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# 集計対象日の決定
# ---------------------------------------------------------------------------
if [[ -z "$DATE_ARG" ]]; then
  DATE_ARG="$(date +%Y%m%d)"
fi

# CREWVIA_REPO_ROOT が設定されていれば優先使用 (worktree 対応)
LOG_DIR="${CREWVIA_REPO_ROOT:-$REPO_ROOT}/logs/dispatcher"
LOG_FILE="${LOG_DIR}/dispatcher-${DATE_ARG}.log"

# ---------------------------------------------------------------------------
# ログ読み込みと通知ライン抽出
# ---------------------------------------------------------------------------
LOG_EXISTS=false
NOTIFY_LINES=""

if [[ -f "$LOG_FILE" ]]; then
  LOG_EXISTS=true
  # 「→ [target] 」パターンの行を抽出 (実際に送信された通知)
  NOTIFY_LINES="$(grep -E '→ \[' "$LOG_FILE" || true)"

  # --last N オプション: 末尾 N 件に絞る
  if [[ -n "$LAST_N" ]]; then
    NOTIFY_LINES="$(echo "$NOTIFY_LINES" | tail -n "$LAST_N")"
  fi
fi

# ①通知送信件数
if [[ -n "$NOTIFY_LINES" ]]; then
  NOTIFY_SENT="$(echo "$NOTIFY_LINES" | wc -l | tr -d ' ')"
else
  NOTIFY_SENT=0
fi

# ③Director 実受信確認件数 (target 名に "director" を含む行)
if [[ -n "$NOTIFY_LINES" ]]; then
  DIRECTOR_RECEIVED="$(echo "$NOTIFY_LINES" | grep -c 'director' || true)"
else
  DIRECTOR_RECEIVED=0
fi

# ---------------------------------------------------------------------------
# ②notify cache ヒット件数 (Python でパース)
# ---------------------------------------------------------------------------
CACHE_EXISTS=false
CACHE_TOTAL=0
CACHE_ACTIVE=0

if [[ -f "$NOTIFY_CACHE" ]]; then
  CACHE_EXISTS=true
  NOW_TS="$(date +%s)"

  read -r CACHE_TOTAL CACHE_ACTIVE <<< "$(python3 - "$NOTIFY_CACHE" "$NOW_TS" "$NOTIFY_TTL" <<'PYEOF'
import json, sys

cache_file = sys.argv[1]
now = float(sys.argv[2])
ttl = float(sys.argv[3])

try:
    with open(cache_file) as f:
        cache = json.load(f)
except Exception:
    print("0 0")
    sys.exit(0)

total = len(cache)
active = sum(
    1 for ts in cache.values()
    if isinstance(ts, (int, float)) and (now - ts) <= ttl
)
print(f"{total} {active}")
PYEOF
)"
fi

# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
if $OUTPUT_JSON; then
  python3 - \
    "$DATE_ARG" "$LOG_FILE" "$LOG_EXISTS" \
    "$NOTIFY_SENT" "$CACHE_TOTAL" "$CACHE_ACTIVE" "$DIRECTOR_RECEIVED" \
    "$CACHE_EXISTS" "$NOTIFY_CACHE" "${LAST_N:-}" \
    <<'PYEOF'
import json, sys

date_arg       = sys.argv[1]
log_file       = sys.argv[2]
log_exists     = sys.argv[3] == "true"
notify_sent    = int(sys.argv[4])
cache_total    = int(sys.argv[5])
cache_active   = int(sys.argv[6])
director_rcvd  = int(sys.argv[7])
cache_exists   = sys.argv[8] == "true"
cache_path     = sys.argv[9]
last_n         = sys.argv[10] or None

result = {
    "date": date_arg,
    "log_file": log_file,
    "log_exists": log_exists,
    "last_n": int(last_n) if last_n else None,
    "stats": {
        "notify_sent":          notify_sent,
        "cache_active_entries": cache_active,
        "cache_total_entries":  cache_total,
        "director_received":    director_rcvd,
    },
    "notify_cache": {
        "path":   cache_path,
        "exists": cache_exists,
    },
}
print(json.dumps(result, ensure_ascii=False, indent=2))
PYEOF

else
  # plain-text サマリー出力
  echo "================================================="
  echo " Dispatcher 通知到達統計 — ${DATE_ARG}"
  echo "================================================="
  echo ""
  echo " ログファイル : $LOG_FILE"
  if ! $LOG_EXISTS; then
    echo " ⚠️  ログファイルが見つかりません"
  fi
  if [[ -n "$LAST_N" ]]; then
    echo " 集計範囲     : 最新 ${LAST_N} 件の通知ライン"
  fi
  echo ""
  echo " ①通知送信件数            : ${NOTIFY_SENT} 件"
  echo " ②notify cache ヒット"
  echo "    - アクティブ (suppress中) : ${CACHE_ACTIVE} 件"
  echo "    - 全エントリ (履歴)        : ${CACHE_TOTAL} 件"
  echo " ③Director 実受信確認      : ${DIRECTOR_RECEIVED} 件"
  echo ""
  if ! $CACHE_EXISTS; then
    echo " ⚠️  notify cache が見つかりません: $NOTIFY_CACHE"
  fi
  echo "================================================="
fi
