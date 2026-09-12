#!/usr/bin/env bash
# scripts/test_review_plan_model.sh
# t001 (mission 20260909-dead-config-sweep) の回帰テスト。
#
# 背景: scripts/review-plan.sh は mux 経路 (L74 付近) と inline フォールバック
# 経路 (L103 付近) の両方で `claude --model claude-opus-4-5` をハードコードして
# おり、config/crewvia.yaml の model_per_skill.plan_review 設定 (claude-opus-5)
# が完全に無視される dead config になっていた。しかも claude-opus-4-5 は存在
# しない世代の ID だった。
#
# 修正: scripts/lib_model.py resolve --skills plan_review 経由でモデルを解決し、
# 空なら config の worker_model にフォールバック、それでも空なら --model を
# 付けない (scripts/start.sh の _resolve_worker_model / MODEL_CLI_ARG と同じ規約)。
#
# テスト方法: scripts/review-plan.sh 本体・scripts/lib_model.py を実ファイルの
# まま scratch ディレクトリにコピーして実行する (ロジックを書き換えずに検証)。
# claude CLI は一切起動しない — mux 経路は mux_spawn に渡された INLINE_CMD
# 文字列そのものを検査し、inline フォールバック経路は claude をスタブに
# 差し替えて実際に渡された argv を検査する。
#
# 検証内容:
#   1. mux 経路: INLINE_CMD に config 通りのモデル ID (--model claude-opus-5)
#      が含まれ、旧ハードコード値 (claude-opus-4-5) が含まれないこと
#   2. inline フォールバック経路: claude スタブに渡る argv が
#      ["--model", "claude-opus-5"] であること (config 通り)
#   3. config を書き換えると解決結果も追従すること (dead config でないことの
#      直接証明) — model_per_skill.plan_review を差し替えたカスタム config で
#      inline フォールバック経路を再実行し、argv がカスタム値に変わること
#   4. model_per_skill.plan_review 自体が未定義の config では worker_model に
#      フォールバックすること (lib_model.py の設計通りの経路を review-plan.sh
#      側が正しく尊重していることの確認)
#   5. 静的回帰ガード: review-plan.sh 内に旧ハードコード
#      (`claude --model claude-opus-4-5` / `claude --model claude-opus-4-5`)
#      が残っていないこと
#
# 実行: bash scripts/test_review_plan_model.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_review_plan_model.sh (t001) =="

# --- scratch 環境の共通セットアップ ---
# review-plan.sh 本体・lib_model.py は実ファイルをそのままコピーする。
_setup_scratch() {
  local dir="$1"
  mkdir -p "$dir/scripts" "$dir/config" "$dir/queue/missions/testmission"
  cp "$REAL_REPO/scripts/review-plan.sh" "$dir/scripts/"
  cp "$REAL_REPO/scripts/lib_model.py" "$dir/scripts/"
  # t018 (mission 20260912-verdict-ci-launcher): review-plan.sh は lib_verdict.py の
  # 終了コード 10 (verdict 行の兆候なし) だけを「判定不能」とみなし、それ以外
  # (スクリプト不在で python3 が返す 2 を含む) は書式違反として fail-closed に
  # するようになった。このテストはモデル解決だけを見るため、以前は暗黙に
  # 依存していた「lib_verdict.py が無い」状態の代わりに、兆候なしを返す
  # スタブを明示的に置く。
  printf '%s\n' 'import sys' 'sys.exit(10)' > "$dir/scripts/lib_verdict.py"
  # wait_for_plan_review.sh は即座に OK を返すスタブに差し替える
  # (ポーリング判定自体は scripts/test_wait_for_plan_review.sh が別途検証済み)。
  cat > "$dir/scripts/wait_for_plan_review.sh" << 'EOF'
#!/usr/bin/env bash
echo "OK"
exit 0
EOF
  chmod +x "$dir/scripts/wait_for_plan_review.sh"
}

echo ""
echo "--- Test 1: mux 経路 — INLINE_CMD が config 通りのモデル ID を使う ---"
TMPDIR1="$(mktemp -d /tmp/crewvia-test-review-plan-model-mux-XXXXXX)"
_setup_scratch "$TMPDIR1"
cp "$REAL_REPO/config/crewvia.yaml" "$TMPDIR1/config/crewvia.yaml"
CMD_LOG="$TMPDIR1/inline_cmd.log"
cat > "$TMPDIR1/scripts/lib_mux.sh" << EOF
mux_available() { return 0; }
mux_spawn() { echo "\$2" > "$CMD_LOG"; return 0; }
mux_kill() { return 0; }
EOF

set +e
bash "$TMPDIR1/scripts/review-plan.sh" testmission > "$TMPDIR1/stdout.log" 2>&1
RC1=$?
set -e

CAPTURED_CMD="$(cat "$CMD_LOG" 2>/dev/null || echo '<missing>')"
if [[ "$RC1" -eq 0 && "$CAPTURED_CMD" == *"claude --model 'claude-opus-5'"* ]]; then
  pass "mux 経路: INLINE_CMD に --model 'claude-opus-5' (config/crewvia.yaml の model_per_skill.plan_review 通り) が含まれる"
else
  fail "mux 経路: 期待したモデル指定が見つからない (rc=$RC1) — captured: $CAPTURED_CMD"
fi
if [[ "$CAPTURED_CMD" != *"claude-opus-4-5"* ]]; then
  pass "mux 経路: 旧ハードコード値 claude-opus-4-5 が残っていない"
else
  fail "mux 経路: 旧ハードコード値 claude-opus-4-5 がまだ INLINE_CMD に含まれている — captured: $CAPTURED_CMD"
fi
rm -rf "$TMPDIR1"

echo ""
echo "--- Test 2: inline フォールバック経路 — claude に渡る argv が config 通りのモデル ID ---"
TMPDIR2="$(mktemp -d /tmp/crewvia-test-review-plan-model-inline-XXXXXX)"
_setup_scratch "$TMPDIR2"
cp "$REAL_REPO/config/crewvia.yaml" "$TMPDIR2/config/crewvia.yaml"
cat > "$TMPDIR2/scripts/lib_mux.sh" << 'EOF'
mux_available() { return 1; }
mux_spawn() { return 1; }
mux_kill() { return 0; }
EOF

FAKE_BIN_DIR="$(mktemp -d /tmp/crewvia-test-review-plan-model-bin-XXXXXX)"
ARGV_LOG="$TMPDIR2/argv.log"
cat > "$FAKE_BIN_DIR/claude" << EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$ARGV_LOG"
cat > "queue/missions/testmission/plan_review.md" << 'INNER'
**Verdict:** approve

# Plan Review: testmission (stub claude)
INNER
exit 0
EOF
chmod +x "$FAKE_BIN_DIR/claude"

set +e
PATH="$FAKE_BIN_DIR:$PATH" bash "$TMPDIR2/scripts/review-plan.sh" testmission > "$TMPDIR2/stdout.log" 2>&1
RC2=$?
set -e

CAPTURED_ARGV="$(cat "$ARGV_LOG" 2>/dev/null | tr '\n' ' ')"
if [[ "$RC2" -eq 0 && "$CAPTURED_ARGV" == *"--model claude-opus-5"* ]]; then
  pass "inline フォールバック経路: claude に --model claude-opus-5 (config 通り) が渡される"
else
  fail "inline フォールバック経路: 期待した argv が見つからない (rc=$RC2) — captured argv: '$CAPTURED_ARGV', stdout: $(cat "$TMPDIR2/stdout.log")"
fi
rm -rf "$FAKE_BIN_DIR"

echo ""
echo "--- Test 3: config の model_per_skill.plan_review を差し替えると解決結果も追従する (dead config でないことの直接証明) ---"
CUSTOM_MODEL="claude-haiku-4-5-20251001"
cat > "$TMPDIR2/config/crewvia.yaml" << EOF
worker_model: claude-sonnet-5
model_per_skill:
  plan_review: $CUSTOM_MODEL
EOF

FAKE_BIN_DIR2="$(mktemp -d /tmp/crewvia-test-review-plan-model-bin2-XXXXXX)"
ARGV_LOG2="$TMPDIR2/argv2.log"
cat > "$FAKE_BIN_DIR2/claude" << EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$ARGV_LOG2"
cat > "queue/missions/testmission/plan_review.md" << 'INNER'
**Verdict:** approve

# Plan Review: testmission (stub claude, custom config)
INNER
exit 0
EOF
chmod +x "$FAKE_BIN_DIR2/claude"

set +e
PATH="$FAKE_BIN_DIR2:$PATH" bash "$TMPDIR2/scripts/review-plan.sh" testmission > "$TMPDIR2/stdout2.log" 2>&1
RC3=$?
set -e

CAPTURED_ARGV2="$(cat "$ARGV_LOG2" 2>/dev/null | tr '\n' ' ')"
if [[ "$RC3" -eq 0 && "$CAPTURED_ARGV2" == *"--model $CUSTOM_MODEL"* ]]; then
  pass "config の model_per_skill.plan_review をカスタム値 ($CUSTOM_MODEL) に差し替えると argv も追従する"
else
  fail "config を差し替えても解決結果が追従しない (dead config の疑い) — captured argv: '$CAPTURED_ARGV2'"
fi
rm -rf "$FAKE_BIN_DIR2"

echo ""
echo "--- Test 4: model_per_skill に plan_review が未定義の config では worker_model にフォールバックする ---"
cat > "$TMPDIR2/config/crewvia.yaml" << 'EOF'
worker_model: claude-sonnet-5
model_per_skill:
  docs: claude-haiku-4-5-20251001
EOF

FAKE_BIN_DIR3="$(mktemp -d /tmp/crewvia-test-review-plan-model-bin3-XXXXXX)"
ARGV_LOG3="$TMPDIR2/argv3.log"
cat > "$FAKE_BIN_DIR3/claude" << EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$ARGV_LOG3"
cat > "queue/missions/testmission/plan_review.md" << 'INNER'
**Verdict:** approve

# Plan Review: testmission (stub claude, fallback config)
INNER
exit 0
EOF
chmod +x "$FAKE_BIN_DIR3/claude"

set +e
PATH="$FAKE_BIN_DIR3:$PATH" bash "$TMPDIR2/scripts/review-plan.sh" testmission > "$TMPDIR2/stdout3.log" 2>&1
RC4=$?
set -e

CAPTURED_ARGV3="$(cat "$ARGV_LOG3" 2>/dev/null | tr '\n' ' ')"
if [[ "$RC4" -eq 0 && "$CAPTURED_ARGV3" == *"--model claude-sonnet-5"* ]]; then
  pass "model_per_skill.plan_review 未定義時は worker_model (claude-sonnet-5) にフォールバックする"
else
  fail "worker_model フォールバックが機能していない — captured argv: '$CAPTURED_ARGV3'"
fi
rm -rf "$FAKE_BIN_DIR3"
rm -rf "$TMPDIR2"

echo ""
echo "--- Test 5 (静的回帰ガード): review-plan.sh に旧ハードコードのコマンド形が残っていないこと ---"
# 「claude-opus-4-5」という文字列自体は本修正の背景コメントに残る (意図的な
# ドキュメンテーション) ため、実際にハードコードされていたコマンド形
# (`claude --model claude-opus-4-5`) だけを対象にする。
if timeout 30 grep -q -- "claude --model claude-opus-4-5" "$REAL_REPO/scripts/review-plan.sh"; then
  fail "review-plan.sh に旧ハードコードのコマンド (claude --model claude-opus-4-5) がまだ残っている"
else
  pass "review-plan.sh に旧ハードコードのコマンド (claude --model claude-opus-4-5) は残っていない"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
