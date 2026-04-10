#!/bin/bash
# classify_sh.sh — .sh ファイルのセキュリティ分類エンジン
# Usage: ./scripts/shared/classify_sh.sh <file>
# Exit:  0 = SAFE, 1 = BLOCK
# Stdout: "SAFE" または "BLOCK: <reason>"
set -euo pipefail

FILE="${1:?Usage: $0 <file>}"
[[ -f "$FILE" ]] || { echo "BLOCK: file not found: $FILE"; exit 1; }

# 自己参照は常に SAFE
SELF_REAL=$(realpath "$0" 2>/dev/null || echo "$0")
FILE_REAL=$(realpath "$FILE" 2>/dev/null || echo "$FILE")
[[ "$FILE_REAL" == "$SELF_REAL" ]] && { echo "SAFE: classifier itself"; exit 0; }

# ---- 前処理: コメント行除去・文字列リテラル空白化 ----
STRIPPED=$(python3 - "$FILE" <<'PYEOF'
import sys, re
with open(sys.argv[1]) as f:
    content = f.read()
lines = []
for line in content.splitlines():
    stripped_line = line.strip()
    if stripped_line.startswith('#!'):
        lines.append(line)
        continue
    if stripped_line.startswith('#'):
        continue
    # 単純な文字列リテラルを空白化（ネストなし）
    line = re.sub(r"'[^'\n]*'", "''", line)
    line = re.sub(r'"[^"\n]*"', '""', line)
    lines.append(line)
print('\n'.join(lines))
PYEOF
)

_block() { echo "BLOCK: $1"; exit 1; }

# ---- Stage 1: 絶対ブロック ----
echo "$STRIPPED" | grep -qE 'curl[^|#]*\|[[:space:]]*(bash|sh)\b'           && _block "curl|bash (RCE)"
echo "$STRIPPED" | grep -qE 'wget[^|#]*\|[[:space:]]*(bash|sh)\b'           && _block "wget|bash (RCE)"
echo "$STRIPPED" | grep -qE 'bash[[:space:]]+<\([[:space:]]*(curl|wget)'    && _block "bash <(curl) (RCE)"
echo "$STRIPPED" | grep -qE 'eval[[:space:]]*\$\((curl|wget)'               && _block "eval \$(curl) (RCE)"
echo "$STRIPPED" | grep -qE '\bsudo\b'                                       && _block "sudo"
echo "$STRIPPED" | grep -qE 'rm[[:space:]]+-[rf]*r[rf]*[[:space:]]+/[^t/]'  && _block "rm -rf on non-/tmp absolute path"
echo "$STRIPPED" | grep -qE 'rm[[:space:]]+-[rf]*r[rf]*[[:space:]]+/\*'     && _block "rm -rf /*"
echo "$STRIPPED" | grep -qE 'dd[[:space:]]+if=.*of=/dev/'                   && _block "dd to device"
echo "$STRIPPED" | grep -qE '\bmkfs\b'                                       && _block "mkfs"
echo "$STRIPPED" | grep -qE ':\(\)\{[[:space:]]*:[[:space:]]*\|[[:space:]]*:&' && _block "fork bomb"

# ---- Stage 2: rm -rf "$VAR" のコンテキスト判定 ----
# 変数名抽出は元ファイルから（STRIPPED は引用符内の変数名も除去するため）
RM_VARS=$(grep -v '^[[:space:]]*#' "$FILE" \
  | grep -oE 'rm[[:space:]]+-[rf]+[[:space:]]+"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"?' \
  | grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*\}?' \
  | tr -d '${}' | sort -u 2>/dev/null || true)

for var in $RM_VARS; do
  # 位置パラメータ ($1/$@/$* 等) 由来 → BLOCK
  if grep -qE "${var}[[:space:]]*=.*\\\$[@*]" "$FILE" 2>/dev/null || \
     grep -qE "${var}[[:space:]]*=.*\\\$[0-9]" "$FILE" 2>/dev/null; then
    _block "rm -rf \$$var (external input via positional param)"
  fi
  # /tmp/ または /var/ への直接代入 → SAFE
  grep -qE "${var}[[:space:]]*=.*(/tmp/|/var/)" "$FILE" 2>/dev/null && continue
  # mktemp 由来 → SAFE (/tmp に作成される)
  grep -qE "${var}[[:space:]]*=.*mktemp" "$FILE" 2>/dev/null && continue
  # 1段間接: VAR="${PARENT}/sub" で PARENT が /tmp → SAFE
  parent_var=$(grep -oE "${var}[[:space:]]*=[[:space:]]*\"?\\\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?" "$FILE" 2>/dev/null \
    | grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*\}?' | tr -d '${}' | head -1 || true)
  if [[ -n "$parent_var" ]]; then
    grep -qE "${parent_var}[[:space:]]*=.*(/tmp/|/var/)" "$FILE" 2>/dev/null && continue
    grep -qE "${parent_var}[[:space:]]*=.*mktemp" "$FILE" 2>/dev/null && continue
  fi
  _block "rm -rf \$$var (not a /tmp or /var constant)"
done

# ---- Stage 3: eval "$VAR" のコンテキスト判定 ----
# 変数名抽出は元ファイルから（STRIPPED は引用符内の変数名も除去するため）
EVAL_VARS=$(grep -v '^[[:space:]]*#' "$FILE" \
  | grep -oE 'eval[[:space:]]+"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"?' \
  | grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*\}?' \
  | tr -d '${}' | sort -u 2>/dev/null || true)

for var in $EVAL_VARS; do
  if grep -qE "${var}[[:space:]]*=.*\\\$[@*]" "$FILE" 2>/dev/null || \
     grep -qE "${var}[[:space:]]*=.*\\\$[0-9]" "$FILE" 2>/dev/null; then
    _block "eval \$$var (external input)"
  fi
  grep -qE "${var}[[:space:]]*=[[:space:]]*['\"]" "$FILE" 2>/dev/null && continue
  _block "eval \$$var (not a string constant)"
done

echo "SAFE"
exit 0
