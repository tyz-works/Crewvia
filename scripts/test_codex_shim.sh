#!/usr/bin/env bash
# test_codex_shim.sh — scripts/bin/codex の回帰テスト（PR #352 attempt 2 / t007 レビュー差し戻し）
#
# P1 (必須・merge を止めた理由): shim の最終行は `codex` を bare name で nvm-exec に渡す。
# nvm-exec は PATH を置き換えず、解決された node バージョンの bin を**継承 PATH の先頭に足すだけ**
# なので、crewvia の文脈では scripts/bin（= shim 自身のディレクトリ）が PATH に残り続ける。
# 解決先のバージョンに codex が無いと、bare `codex` の解決が shim 自身に戻る
# → shim → nvm-exec → shim → … の無限ループ（全段 exec なのでプロセス数は増えないが、
# 出力 0 バイトのまま無言で返ってこない。実測: exit=124 / timeout で kill）。
# 修正: nvm-exec に渡す前に自分のディレクトリを PATH から外す。これで codex が無いときは
# nvm-exec の exec が `codex: not found` (exit 127) を返し、kai-review.sh の
# `set -euo pipefail` が CODEX FAILURE: exit=127 → fail_needs_director に倒す
# （= 意図どおりの fail-closed）。
#
# 無界の再帰をそのまま走らせず、`timeout` で有界化する（task の指示どおり）。
# 本物の nvm は使わず、nvm-exec の「継承 PATH の先頭に node バージョンの bin を足して
# bare name を exec する」という観測済みの挙動だけを偽の nvm-exec で模す。
#
# P3 (軽微): NVM_EXEC が $HOME/.nvm 決め打ちで $NVM_DIR を見ていなかった（nvm 自身の規約は
# $NVM_DIR）。これも検証する。
#
# 実行: bash scripts/test_codex_shim.sh
# 副作用: /tmp 配下に一時ディレクトリを作成し終了時に削除する

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CODEX_SHIM="$OWN_CHECKOUT_ROOT/scripts/bin/codex"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_codex_shim.sh (scripts/bin/codex: self-recursion P1 / NVM_DIR P3) =="

TMPDIR_TEST="$(mktemp -d)"
SHIMBIN="$TMPDIR_TEST/shimbin"
NODEBIN="$TMPDIR_TEST/fakenode/bin"
HOME_DIR="$TMPDIR_TEST/home"
mkdir -p "$SHIMBIN" "$NODEBIN" "$HOME_DIR/.nvm"

# shim 自身のコピー（scripts/bin 丸ごとに他の shim (plan/python3) が無い、独立した PATH 要素にする）
cp "$CODEX_SHIM" "$SHIMBIN/codex"
chmod +x "$SHIMBIN/codex"

# 偽 nvm-exec: 実物の観測済みの挙動 (PATH を置き換えず、解決した node バージョンの bin を
# 継承 PATH の先頭に足してから bare name で exec する) だけを模す。$NODEBIN には codex を
# 置かない (= 「バージョンは解決できるが codex が無い」場面)。
# quoted heredoc ('FAKE_NVM_EXEC'): シェル変数展開を一切せず書き出し、$NODEBIN は後から
# sed で埋める (unquoted heredoc + \$ エスケープにすると、tests/test_task_status_single_definition.py
# の .sh heredoc スキャナが compile 時の SyntaxWarning を拾ってノイズになるため)。
cat > "$HOME_DIR/.nvm/nvm-exec" <<'FAKE_NVM_EXEC'
#!/bin/sh
PATH="__NODEBIN__:$PATH"
export PATH
exec "$@"
FAKE_NVM_EXEC
sed -i "s#__NODEBIN__#$NODEBIN#" "$HOME_DIR/.nvm/nvm-exec"
chmod +x "$HOME_DIR/.nvm/nvm-exec"

# ---------------------------------------------------------------------------
# Test 1: 修正後の shim は無言でハングせず、短時間で非ゼロ終了する
# ---------------------------------------------------------------------------
out=$(timeout -s KILL 5 env -i HOME="$HOME_DIR" PATH="$SHIMBIN:/usr/bin:/bin" "$SHIMBIN/codex" --version 2>&1)
rc=$?
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  fail "fixed shim hung (timeout killed it, exit=$rc) — self-recursion regression"
elif [ "$rc" -eq 0 ]; then
  fail "fixed shim exited 0 with no real codex on PATH (expected a failure, not success): $out"
else
  pass "fixed shim fails fast and non-zero when resolved node has no codex (exit=$rc)"
fi

# ---------------------------------------------------------------------------
# Test 2: 上と同じ場面で、修正前 (自分のディレクトリを PATH から外さない版) は無限に
# 再帰してハングすることを示す (このテストが差し戻しの原因を捕まえることの実証)。
# 無界のまま走らせず timeout で有界化する。
# ---------------------------------------------------------------------------
PRE_FIX_SHIM="$TMPDIR_TEST/shimbin-prefix/codex"
mkdir -p "$(dirname "$PRE_FIX_SHIM")"
grep -v -e '^SELF_DIR=' -e '^PATH=\$(printf' -e '^export PATH$' "$SHIMBIN/codex" \
  | sed -e 's/^NVM_EXEC="\${NVM_DIR:-\$HOME\/.nvm}\/nvm-exec"$/NVM_EXEC="$HOME\/.nvm\/nvm-exec"/' \
  > "$PRE_FIX_SHIM"
chmod +x "$PRE_FIX_SHIM"

timeout -s KILL 5 env -i HOME="$HOME_DIR" PATH="$(dirname "$PRE_FIX_SHIM"):/usr/bin:/bin" "$PRE_FIX_SHIM" --version >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  pass "pre-fix shim (no self-dir strip) reproduces the hang under timeout (exit=$rc) — confirms the regression test bites"
else
  fail "pre-fix shim did not hang as expected (exit=$rc) — the repro setup itself may be wrong"
fi

# ---------------------------------------------------------------------------
# Test 3: NVM_DIR が設定されているとき、そこの nvm-exec を使う ($HOME/.nvm 決め打ちではない)
# ---------------------------------------------------------------------------
ALT_NVM_DIR="$TMPDIR_TEST/alt-nvm"
mkdir -p "$ALT_NVM_DIR"
cat > "$ALT_NVM_DIR/nvm-exec" <<'EOF'
#!/bin/sh
echo "alt-nvm-exec ran"
exit 0
EOF
chmod +x "$ALT_NVM_DIR/nvm-exec"

out=$(env -i HOME="$TMPDIR_TEST/no-such-home" NVM_DIR="$ALT_NVM_DIR" PATH="$SHIMBIN:/usr/bin:/bin" "$SHIMBIN/codex" --version 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && [[ "$out" == *"alt-nvm-exec ran"* ]]; then
  pass "shim honors \$NVM_DIR instead of hardcoding \$HOME/.nvm"
else
  fail "shim did not honor \$NVM_DIR (rc=$rc, out=$out)"
fi

# ---------------------------------------------------------------------------
# Test 4: nvm-exec が本当に不在のときは今までどおり fail-closed (exit 127, 即座)
# ---------------------------------------------------------------------------
EMPTY_HOME="$TMPDIR_TEST/empty-home"
mkdir -p "$EMPTY_HOME"
out=$(env -i HOME="$EMPTY_HOME" PATH="$SHIMBIN:/usr/bin:/bin" "$SHIMBIN/codex" --version 2>&1)
rc=$?
if [ "$rc" -eq 127 ] && [[ "$out" == *"not found or not executable"* ]]; then
  pass "missing nvm-exec still fails closed (exit 127, clear message)"
else
  fail "missing nvm-exec behavior regressed (rc=$rc, out=$out)"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
