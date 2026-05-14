#!/bin/bash
# sh_perm_manager.sh — PostToolUse hook: .sh 変更後に allow list を自動更新
# stdin: hook JSON ペイロード
set -euo pipefail

# hook JSON からファイルパスを取得
JSON_INPUT=$(cat 2>/dev/null || true)
if [[ -z "$JSON_INPUT" ]]; then exit 0; fi

FILE_PATH=$(echo "$JSON_INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ti = d.get('tool_input', {})
print(ti.get('file_path', ''))
" 2>/dev/null || true)

# .sh ファイルでなければスキップ
[[ -z "$FILE_PATH" ]] && exit 0
[[ "$FILE_PATH" != *.sh ]] && exit 0
[[ -f "$FILE_PATH" ]] || exit 0

# プロジェクトルートを特定（hooks/ の親）
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$HOOK_DIR/.." && pwd)"

# PROJECT_ROOT 外の .sh はスキップ
[[ "$FILE_PATH" != "$PROJECT_ROOT"/* ]] && exit 0

CLASSIFY="$PROJECT_ROOT/scripts/shared/classify_sh.sh"
SETTINGS="$PROJECT_ROOT/.claude/settings.json"
AUDIT_LOG="$PROJECT_ROOT/logs/sh_perm_audit.log"
SNAPSHOT="$PROJECT_ROOT/logs/sh_perm_snapshot.json"

[[ -f "$CLASSIFY" ]] || exit 0
[[ -f "$SETTINGS" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

TS=$(date '+%Y-%m-%dT%H:%M:%S')
REL_PATH="${FILE_PATH#$PROJECT_ROOT/}"

# 分類実行
REASON=$(bash "$CLASSIFY" "$FILE_PATH" 2>/dev/null || true)
if bash "$CLASSIFY" "$FILE_PATH" >/dev/null 2>&1; then
  CLASSIFICATION="SAFE"
else
  CLASSIFICATION="BLOCK"
fi

script_name=$(basename "$FILE_PATH")
script_rel_dir=$(dirname "$REL_PATH")
ENTRY_1="Bash(./${script_rel_dir}/${script_name}:*)"
ENTRY_2="Bash(bash ${script_rel_dir}/${script_name}:*)"
ENTRY_3="Bash(chmod +x ./${script_rel_dir}/${script_name})"
ENTRY_4="Bash(chmod +x ${script_rel_dir}/${script_name})"

# 現在の登録状態を確認
IS_REGISTERED=$(jq --arg e "$ENTRY_1" \
  '[.permissions.allow[] | select(. == $e)] | length' \
  "$SETTINGS" 2>/dev/null || echo "0")

if [[ "$CLASSIFICATION" == "SAFE" && "$IS_REGISTERED" == "0" ]]; then
  # SAFE かつ未登録 → 追加
  BACKUP="${SETTINGS}.bak.hook.$$"
  cp "$SETTINGS" "$BACKUP"
  TMP_FILE=$(mktemp "$(dirname "$SETTINGS")/sh_perm_XXXXXX.json")
  python3 - "$SETTINGS" "$ENTRY_1" "$ENTRY_2" "$ENTRY_3" "$ENTRY_4" <<'PYEOF' > "$TMP_FILE"
import sys, json
settings_file, *new_entries = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
with open(settings_file) as f:
    data = json.load(f)
allow = data.setdefault("permissions", {}).setdefault("allow", [])
for e in new_entries:
    if e not in allow:
        allow.append(e)
print(json.dumps(data, indent=2, ensure_ascii=False))
PYEOF
  if jq . "$TMP_FILE" >/dev/null 2>&1; then
    mv "$TMP_FILE" "$SETTINGS"
    rm -f "$BACKUP"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    printf '%s\tADDED\t%s\tSAFE\n' "$TS" "$REL_PATH" >> "$AUDIT_LOG"
    echo "[sh_perm_manager] ADDED: $REL_PATH" >&2
    # スナップショット更新
    NEW_SHA=$(shasum -a 256 "$FILE_PATH" | awk '{print $1}')
    python3 - "$SNAPSHOT" "$REL_PATH" "$NEW_SHA" "$TS" <<'PYEOF' || true
import sys, json, pathlib
snap_file, rel_path, sha, ts = sys.argv[1:]
p = pathlib.Path(snap_file)
snap = json.loads(p.read_text()) if p.exists() else {}
snap[rel_path] = {"sha256": sha, "classification": "SAFE", "classified_at": ts, "verified_at": ts}
p.write_text(json.dumps(snap, indent=2, ensure_ascii=False))
PYEOF
  else
    cp "$BACKUP" "$SETTINGS"
    rm -f "$BACKUP" "$TMP_FILE"
    printf '%s\tERROR\t%s\tjson invalid after add\n' "$TS" "$REL_PATH" >> "$AUDIT_LOG"
  fi

elif [[ "$CLASSIFICATION" == "BLOCK" && "$IS_REGISTERED" != "0" ]]; then
  # BLOCK かつ登録済み → 除去
  BACKUP="${SETTINGS}.bak.hook.$$"
  cp "$SETTINGS" "$BACKUP"
  TMP_FILE=$(mktemp "$(dirname "$SETTINGS")/sh_perm_XXXXXX.json")
  python3 - "$SETTINGS" "$script_name" <<'PYEOF' > "$TMP_FILE"
import sys, json
settings_file, sname = sys.argv[1], sys.argv[2]
with open(settings_file) as f:
    data = json.load(f)
allow = data.get("permissions", {}).get("allow", [])
data["permissions"]["allow"] = [
    e for e in allow
    if not (isinstance(e, str) and e.startswith("Bash(") and sname in e)
]
print(json.dumps(data, indent=2, ensure_ascii=False))
PYEOF
  if jq . "$TMP_FILE" >/dev/null 2>&1; then
    mv "$TMP_FILE" "$SETTINGS"
    rm -f "$BACKUP"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    printf '%s\tREVOKED\t%s\t%s\n' "$TS" "$REL_PATH" "$REASON" >> "$AUDIT_LOG"
    echo "[sh_perm_manager] REVOKED: $REL_PATH — $REASON" >&2
    # スナップショット更新
    NEW_SHA=$(shasum -a 256 "$FILE_PATH" | awk '{print $1}')
    python3 - "$SNAPSHOT" "$REL_PATH" "$NEW_SHA" "$TS" <<'PYEOF' || true
import sys, json, pathlib
snap_file, rel_path, sha, ts = sys.argv[1:]
p = pathlib.Path(snap_file)
snap = json.loads(p.read_text()) if p.exists() else {}
entry = snap.get(rel_path, {})
entry.update({"sha256": sha, "classification": "BLOCK", "verified_at": ts})
snap[rel_path] = entry
p.write_text(json.dumps(snap, indent=2, ensure_ascii=False))
PYEOF
  else
    cp "$BACKUP" "$SETTINGS"
    rm -f "$BACKUP" "$TMP_FILE"
    printf '%s\tERROR\t%s\tjson invalid after remove\n' "$TS" "$REL_PATH" >> "$AUDIT_LOG"
  fi
fi

exit 0
