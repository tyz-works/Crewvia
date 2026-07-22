#!/usr/bin/env bash
# 新しい環境 (別マシン / WSL) に crewvia を clone した後の初期化スクリプト
#
# 実行例:
#   cd ~/workspace/crewvia
#   ./scripts/setup-new-env.sh
#
# やること:
#   1. .claude/settings.json 内の hook 絶対パスを新環境のパスに書き換え
#   2. hooks/*.sh scripts/*.sh に実行権限を付与
#   3. 前提ツールの有無をチェック
#   4. WSL 特有の落とし穴を検知して警告

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS_JSON="$REPO_ROOT/.claude/settings.json"

echo "== crewvia setup for new environment =="
echo "REPO_ROOT: $REPO_ROOT"
echo

# ---- 1. settings.json のパス書き換え ----
if [ -f "$SETTINGS_JSON" ]; then
  # 現在埋まっているパスを抽出 (hooks/*.sh を指す prefix を検出)
  OLD_PATHS=$(grep -oE '"/[^"]*/hooks/[a-z_-]+\.sh"' "$SETTINGS_JSON" \
    | sed -E 's|"([^"]*)/hooks/[^"]*"|\1|' | sort -u || true)

  if [ -z "$OLD_PATHS" ]; then
    echo "[skip] settings.json 内に hook パスが見つかりません"
  else
    cp "$SETTINGS_JSON" "$SETTINGS_JSON.bak"
    echo "[backup] $SETTINGS_JSON.bak"
    while IFS= read -r old; do
      [ -z "$old" ] && continue
      if [ "$old" = "$REPO_ROOT" ]; then
        echo "[skip] 既に $REPO_ROOT を指しています"
        continue
      fi
      echo "[rewrite] $old -> $REPO_ROOT"
      sed -i "s|${old}/hooks/|${REPO_ROOT}/hooks/|g" "$SETTINGS_JSON"
    done <<< "$OLD_PATHS"

    # JSON 構文チェック
    if command -v jq >/dev/null 2>&1; then
      if jq empty "$SETTINGS_JSON" 2>/dev/null; then
        echo "[ok] settings.json は valid JSON"
      else
        echo "[error] settings.json が壊れました。バックアップから戻します"
        mv "$SETTINGS_JSON.bak" "$SETTINGS_JSON"
        exit 1
      fi
    fi
  fi
else
  echo "[warn] .claude/settings.json が存在しません"
fi
echo

# ---- 2. 実行権限の付与 ----
echo "[chmod] hooks/*.sh scripts/*.sh に実行権限を付与"
chmod +x "$REPO_ROOT"/hooks/*.sh "$REPO_ROOT"/scripts/*.sh 2>/dev/null || true
echo

# ---- 3. 前提ツールのチェック ----
echo "== 前提ツールチェック =="
missing=()
for tool in bash git curl jq python3 tmux gh claude; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf "  [ok]   %-10s %s\n" "$tool" "$(command -v "$tool")"
  else
    printf "  [MISS] %-10s (未インストール)\n" "$tool"
    missing+=("$tool")
  fi
done
# dashboard を使うなら追加
for tool in fzf gum yq; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf "  [ok]   %-10s %s (optional)\n" "$tool" "$(command -v "$tool")"
  else
    printf "  [opt]  %-10s (dashboard/yq 機能で使用)\n" "$tool"
  fi
done
echo

# ---- 4. WSL 環境チェック ----
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "== WSL 環境検知 =="
  # ext4 側かチェック
  case "$REPO_ROOT" in
    /mnt/*)
      echo "  [WARN] $REPO_ROOT は Windows FS (/mnt/*) 配下です"
      echo "         chmod +x が効かず hook が失敗する可能性大 → ext4 側 (~/workspace/) に移してください"
      ;;
    *)
      echo "  [ok] ext4 側に配置されています"
      ;;
  esac

  # core.autocrlf
  autocrlf=$(git -C "$REPO_ROOT" config --get core.autocrlf 2>/dev/null || echo "unset")
  if [ "$autocrlf" = "true" ]; then
    echo "  [WARN] git core.autocrlf=true です。.sh が CRLF 化して壊れる可能性あり"
    echo "         推奨: git config --global core.autocrlf input"
  else
    echo "  [ok] core.autocrlf=$autocrlf"
  fi

  # 実行権限が実際に付いたか (DrvFs だと chmod が無視される)
  if [ ! -x "$REPO_ROOT/scripts/start.sh" ]; then
    echo "  [WARN] scripts/start.sh に実行権限が付きません (FS の問題)"
  fi
  echo
fi

# ---- サマリー ----
echo "== 完了 =="
if [ ${#missing[@]} -gt 0 ]; then
  echo "[要インストール] ${missing[*]}"
  echo
  echo "Ubuntu/Debian の例:"
  echo "  sudo apt install -y jq tmux curl python3"
  echo "  # gh: https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
  echo "  # claude: https://docs.claude.com/claude-code"
  exit 1
fi

echo "すべて OK。次のステップ:"
echo "  - Taskvia を使う場合: TASKVIA_URL / TASKVIA_TOKEN を .env や shell rc に設定"
echo "  - スタンドアロン運用の場合: CREWVIA_TASKVIA=disabled を設定"
echo "  - 起動: ./scripts/start.sh"
