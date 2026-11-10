#!/usr/bin/env bash
# scripts/test_migrate_local_config.sh — scripts/migrate-local-config.sh の回帰テスト (machine-local-config)。
#
# 使い捨ての git repo (mktemp) に fixture の config/crewvia.yaml をコミットし、scripts/migrate-local-config.sh と
# lib_config.py / lib_task_cards.py をコピーして走らせる。HOME も使い捨てにするので、退避先
# (~/crewvia-local-migrate-*) も本番の HOME には作らない。本番の config / registry には触れない。
#
#   (a) 手元変更あり → local ができ・共通が HEAD に戻り・dump が一致・mode が明示される・秘密の値が stdout に出ない
#   (b) 手元変更なし → mode だけの local
#   (c) 既に local がある → 何も変えず exit 1
#   (d) 許可外キー (attempts) の手元変更 → 止めて何も変えない (exit 2)
#   (e) キーを消した手元変更 → exit 2 で何も変えない
#   (f) 型違いの手元変更 (telegram: dict → scalar) → exit 3 で何も変えない
#   (g) 適用後に不一致 (post-checkout hook で共通を書き換える) → exit 4 で手元版に戻る
#   (h) 主 checkout でない (git worktree) → exit 1 で何も変えない
#
# 実行: bash scripts/test_migrate_local_config.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=../tests/fixture_tree.sh
source "$REAL_REPO/tests/fixture_tree.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

WORK="$(mktemp -d /tmp/crewvia-test-migrate-local-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# 隔離: 実機のテスト印・git の env を引き継がない
unset CREWVIA_MUX_TEST_ISOLATION GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

SECRET_MARK="SECRETMARK-op-ref-9f3a"

# make_repo <name> : fixture repo を作って絶対パスを MADE に入れる (HOME は <repo>.home)
make_repo() {
  local name="$1"
  MADE="$WORK/$name"
  mkdir -p "$MADE/config" "$MADE/scripts" "$WORK/$name.home"
  copy_scripts_libs "$REAL_REPO" "$MADE" || { echo "copy_scripts_libs failed" >&2; exit 1; }
  cp "$REAL_REPO/scripts/migrate-local-config.sh" "$MADE/scripts/"
  chmod +x "$MADE/scripts/migrate-local-config.sh"
  printf '%s\n' \
    'mode: hc  # fixture' \
    'wip_limit: 8' \
    'attempts:' \
    '  max_total: 6' \
    'telegram:' \
    '  enabled: false' \
    > "$MADE/config/crewvia.yaml"
  (
    cd "$MADE" || exit 1
    git init -q .
    git add -A
    git -c user.name=t -c user.email=t@example.com commit -q -m fixture
  ) || { echo "git init failed" >&2; exit 1; }
}

# run_migrate <repo> : stdout+stderr を $WORK/out.txt に、終了コードを RC に
run_migrate() {
  local repo="$1"
  RC=0
  (cd "$repo" && HOME="$repo.home" bash scripts/migrate-local-config.sh) > "$WORK/out.txt" 2>&1 || RC=$?
}

dump_json() { (cd "$1" && python3 scripts/lib_config.py dump --json); }

echo "== test_migrate_local_config.sh =="

echo ""
echo "--- (a) 手元変更あり ---"
make_repo a
printf '%s\n' \
  'mode: hc  # fixture' \
  'wip_limit: 3' \
  'attempts:' \
  '  max_total: 6' \
  'telegram:' \
  '  enabled: true' \
  '  credentials:' \
  '    source: op' \
  "    ref: op://vault/item/$SECRET_MARK" \
  > "$MADE/config/crewvia.yaml"
REPO_A="$MADE"
BEFORE_A="$(dump_json "$REPO_A")"
run_migrate "$REPO_A"
AFTER_A="$(dump_json "$REPO_A")"
[[ "$RC" -eq 0 ]] && pass "exit 0" || { fail "exit $RC (期待 0)"; cat "$WORK/out.txt"; }
[[ -f "$REPO_A/config/crewvia.local.yaml" ]] && pass "config/crewvia.local.yaml ができた" || fail "local ができていない"
[[ -z "$(cd "$REPO_A" && git status --short config/crewvia.yaml)" ]] && pass "共通 config/crewvia.yaml が HEAD に戻った" || fail "共通が HEAD と違う"
[[ "$BEFORE_A" == "$AFTER_A" && -n "$BEFORE_A" ]] && pass "移行前後の dump が一致" || fail "dump が一致しない"
grep -q '^mode: hc$' "$REPO_A/config/crewvia.local.yaml" && pass "mode が local に明示された" || fail "local に mode が無い"
grep -q '^wip_limit: 3$' "$REPO_A/config/crewvia.local.yaml" && pass "wip_limit の手元変更が local に入った" || fail "wip_limit が local に無い"
grep -q "$SECRET_MARK" "$REPO_A/config/crewvia.local.yaml" && pass "telegram の手元変更が local に入った" || fail "telegram が local に無い"
if grep -q "$SECRET_MARK" "$WORK/out.txt"; then fail "秘密の値が標準出力に出た"; else pass "秘密の値は標準出力に出ない"; fi
grep -q '^SAME$' "$WORK/out.txt" && pass "SAME を表示した" || fail "SAME が出ていない"
PERM="$(stat -c %a "$REPO_A/config/crewvia.local.yaml" 2>/dev/null || stat -f %Lp "$REPO_A/config/crewvia.local.yaml")"
[[ "$PERM" == "600" ]] && pass "local は 0600" || fail "local の mode が $PERM"
BACKUPS=("$REPO_A".home/crewvia-local-migrate-*)
if [[ -f "${BACKUPS[0]}/crewvia.yaml.local-edited" ]]; then pass "退避先に手元版がある"; else fail "退避先が無い"; fi

echo ""
echo "--- (b) 手元変更なし → mode だけの local ---"
make_repo b
REPO_B="$MADE"
BEFORE_B="$(dump_json "$REPO_B")"
run_migrate "$REPO_B"
AFTER_B="$(dump_json "$REPO_B")"
[[ "$RC" -eq 0 ]] && pass "exit 0" || { fail "exit $RC (期待 0)"; cat "$WORK/out.txt"; }
KEYS_B="$(grep -E '^[A-Za-z_]' "$REPO_B/config/crewvia.local.yaml" | sed 's/:.*//' | tr '\n' ' ')"
[[ "$KEYS_B" == "mode " ]] && pass "local のトップキーは mode だけ" || fail "local のトップキー: '$KEYS_B'"
[[ "$BEFORE_B" == "$AFTER_B" ]] && pass "dump が一致" || fail "dump が一致しない"

echo ""
echo "--- (c) 既に local がある → 何も変えず exit 1 ---"
make_repo c
REPO_C="$MADE"
echo '# existing' > "$REPO_C/config/crewvia.local.yaml"
sed -i 's/^wip_limit: 8$/wip_limit: 5/' "$REPO_C/config/crewvia.yaml"
cp "$REPO_C/config/crewvia.yaml" "$WORK/c.common.before"
run_migrate "$REPO_C"
[[ "$RC" -eq 1 ]] && pass "exit 1" || fail "exit $RC (期待 1)"
[[ "$(cat "$REPO_C/config/crewvia.local.yaml")" == "# existing" ]] && pass "既存の local は無傷" || fail "既存の local が変わった"
cmp -s "$WORK/c.common.before" "$REPO_C/config/crewvia.yaml" && pass "共通は手元版のまま" || fail "共通が変わった"
ls "$REPO_C".home/crewvia-local-migrate-* > /dev/null 2>&1 && fail "退避先が作られた (何もしないはず)" || pass "退避先を作っていない"

echo ""
echo "--- (d) 許可外キー (attempts) の手元変更 → 止めて何も変えない ---"
make_repo d
REPO_D="$MADE"
sed -i 's/max_total: 6/max_total: 9/' "$REPO_D/config/crewvia.yaml"
cp "$REPO_D/config/crewvia.yaml" "$WORK/d.common.before"
run_migrate "$REPO_D"
[[ "$RC" -eq 2 ]] && pass "exit 2" || fail "exit $RC (期待 2)"
[[ ! -e "$REPO_D/config/crewvia.local.yaml" ]] && pass "local を作っていない" || fail "local ができた"
cmp -s "$WORK/d.common.before" "$REPO_D/config/crewvia.yaml" && pass "共通は手元版のまま" || fail "共通が変わった"
grep -q 'attempts' "$WORK/out.txt" && pass "止めた理由にキー名が出る" || fail "キー名が出ていない"

echo ""
echo "--- (e) キーを消した手元変更 → exit 2 ---"
make_repo e
REPO_E="$MADE"
sed -i '/^wip_limit:/d' "$REPO_E/config/crewvia.yaml"
cp "$REPO_E/config/crewvia.yaml" "$WORK/e.common.before"
run_migrate "$REPO_E"
[[ "$RC" -eq 2 ]] && pass "exit 2" || fail "exit $RC (期待 2)"
[[ ! -e "$REPO_E/config/crewvia.local.yaml" ]] && pass "local を作っていない" || fail "local ができた"
cmp -s "$WORK/e.common.before" "$REPO_E/config/crewvia.yaml" && pass "共通は手元版のまま" || fail "共通が変わった"
grep -q 'wip_limit' "$WORK/out.txt" && pass "消したキー名が出る" || fail "キー名が出ていない"

echo ""
echo "--- (f) 型違いの手元変更 → exit 3 ---"
make_repo f
REPO_F="$MADE"
printf '%s\n' 'mode: hc  # fixture' 'wip_limit: 8' 'attempts:' '  max_total: 6' 'telegram: true' > "$REPO_F/config/crewvia.yaml"
cp "$REPO_F/config/crewvia.yaml" "$WORK/f.common.before"
run_migrate "$REPO_F"
[[ "$RC" -eq 3 ]] && pass "exit 3" || { fail "exit $RC (期待 3)"; cat "$WORK/out.txt"; }
[[ ! -e "$REPO_F/config/crewvia.local.yaml" ]] && pass "local を作っていない" || fail "local ができた"
cmp -s "$WORK/f.common.before" "$REPO_F/config/crewvia.yaml" && pass "共通は手元版のまま" || fail "共通が変わった"

echo ""
echo "--- (g) 適用後に不一致 → exit 4 で手元版に戻る ---"
make_repo g
REPO_G="$MADE"
sed -i 's/^wip_limit: 8$/wip_limit: 3/' "$REPO_G/config/crewvia.yaml"
cp "$REPO_G/config/crewvia.yaml" "$WORK/g.common.before"
# checkout の直後に共通を書き換える hook → 適用後の dump が移行前と食い違う
printf '%s\n' '#!/bin/sh' "printf 'extra_key: 1\\n' >> config/crewvia.yaml" > "$REPO_G/.git/hooks/post-checkout"
chmod +x "$REPO_G/.git/hooks/post-checkout"
run_migrate "$REPO_G"
[[ "$RC" -eq 4 ]] && pass "exit 4" || { fail "exit $RC (期待 4)"; cat "$WORK/out.txt"; }
[[ ! -e "$REPO_G/config/crewvia.local.yaml" ]] && pass "local を消した" || fail "local が残った"
cmp -s "$WORK/g.common.before" "$REPO_G/config/crewvia.yaml" && pass "共通は手元版に戻った" || fail "共通が手元版と違う"

echo ""
echo "--- (h) worktree から実行 → exit 1 ---"
make_repo h
REPO_H="$MADE"
WT_H="$WORK/h.wt"
(cd "$REPO_H" && git worktree add -q "$WT_H" -b wt-h) || { echo "git worktree add failed" >&2; exit 1; }
mkdir -p "$WT_H/scripts"
cp "$REPO_H/scripts/"*.py "$REPO_H/scripts/migrate-local-config.sh" "$WT_H/scripts/" 2>/dev/null
run_migrate "$WT_H"
[[ "$RC" -eq 1 ]] && pass "exit 1" || { fail "exit $RC (期待 1)"; cat "$WORK/out.txt"; }
grep -q '主 checkout' "$WORK/out.txt" && pass "理由に主 checkout が出る" || fail "理由が出ていない"
[[ ! -e "$WT_H/config/crewvia.local.yaml" ]] && pass "worktree に local を作っていない" || fail "local ができた"
ls "$WT_H".home/crewvia-local-migrate-* > /dev/null 2>&1 && fail "退避先が作られた" || pass "退避先を作っていない"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
