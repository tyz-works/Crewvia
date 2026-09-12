#!/usr/bin/env bash
# scripts/test_review_plan_director_identity.sh
#
# t008 (PR#188 レビュー指摘 P1・Codex 指摘、Seo は未検出) の回帰テスト。
#
# 背景: scripts/plan.sh review (= scripts/review-plan.sh) は Director セッションの
# Bash tool 呼び出しから直接起動される。そのため AGENT_NAME を明示的に上書き
# しないと、Director の AGENT_NAME (registry/workers.yaml で role: director) が
# そのまま reviewer の子プロセスに継承される。hooks/pre-tool-use.sh の Director
# bypass (role: director を見て即 allow) は SKILLS=plan_review の per-skill
# チェックより前に評価されるため、上書きが無いと reviewer セッションは
# 「登録済み Director」として Bash/Edit が無制限に通り、この PR (t002) が
# 追加した write-scope guard / skill-permissions.yaml の deny に一度も
# 到達しない (= 本番で発火しない dead code)。
#
# ⚠️ 重要: このテストは「Director セッションから plan.sh review を起動した
# 実運用と同じ経路」を再現する。具体的には:
#   - AGENT_NAME を明示的に外した env は使わない。むしろ逆に、テストの
#     出発点として AGENT_NAME を registry 上の実在する role: director の
#     名前 (例: Sora) に明示的にセットし、「Director セッションの子プロセス
#     として起動された」状態を模す。
#   - scripts/review-plan.sh 本体 (コピーではなく実ファイルをそのまま
#     scratch CREWVIA_DIR にコピーして) を実際に実行し、mux 分岐だけを
#     CREWVIA_HERDR_SOCK (README/CLAUDE.md に明記されたテスト専用の socket
#     上書き変数) で無効化して inline フォールバック経路を通す。
#     claude CLI 自体はスタブに差し替えるが、review-plan.sh 側のロジック
#     (env export の順序・内容) は一切変更せず実ファイルのまま実行する。
#
# 検証内容:
#   1. (再現) AGENT_NAME=<director名> のまま SKILLS=plan_review で hook を
#      直接呼ぶと Bash が無言で bypass (allow) されること — 修正前の事故の
#      再現。これが崩れていたら以降のテストの前提そのものが崩れているので
#      先に検知する。
#   2. (本修正) scripts/review-plan.sh を実際に実行し、Director の
#      AGENT_NAME を継承した状態で起動しても、reviewer の子プロセスに渡る
#      AGENT_NAME が 'Plan-Reviewer' に上書きされていること。
#   3. (本修正) その 'Plan-Reviewer' という AGENT_NAME で hook を呼ぶと
#      Bash が deny されること (Director bypass ではなく SKILLS=plan_review
#      の Bash 全面禁止に到達している)。
#   4. (静的回帰ガード) mux 経路・inline フォールバック経路の両方に
#      AGENT_NAME='Plan-Reviewer' の export が入っていること。
#
# 実行: bash scripts/test_review_plan_director_identity.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

echo "== test_review_plan_director_identity.sh (t008) =="

# registry/workers.yaml から実在する role: director の名前を取得する
# (ハードコードせず、実データに追従する)。
DIRECTOR_NAME="$(awk '
  /^  - name:/ { name = $3 }
  /role: director/ { print name; exit }
' "$REAL_REPO/registry/workers.yaml")"

if [[ -z "$DIRECTOR_NAME" ]]; then
  echo "SKIP: registry/workers.yaml に role: director の worker が見つかりません" >&2
  exit 0
fi
echo "(registry 上の director 名: $DIRECTOR_NAME)"

BASH_TOOL_JSON='{"tool_name":"Bash","tool_input":{"command":"echo hi"}}'

echo ""
echo "--- Test 1 (前提の再現): AGENT_NAME=<director名> のまま SKILLS=plan_review で hook を呼ぶと Bash が bypass (allow) される ---"
OUT1="$(echo "$BASH_TOOL_JSON" | AGENT_NAME="$DIRECTOR_NAME" SKILLS=plan_review CREWVIA_REPO="$REAL_REPO" bash "$REAL_REPO/hooks/pre-tool-use.sh" 2>/dev/null)"
if [[ -z "$OUT1" ]]; then
  pass "AGENT_NAME=$DIRECTOR_NAME (上書きなし) では Bash が無言で bypass される — 修正前の事故を再現できた"
else
  fail "前提が再現できなかった (bypass されず何か出力された): $OUT1"
fi

echo ""
echo "--- Test 2 (本修正・実運用経路): review-plan.sh を Director セッション相当の env から実行し、reviewer 子プロセスの AGENT_NAME が上書きされることを確認 ---"

SCRATCH_DIR="$(mktemp -d /tmp/crewvia-test-review-plan-XXXXXX)"
trap 'rm -rf "$SCRATCH_DIR"' EXIT

mkdir -p "$SCRATCH_DIR/scripts"
mkdir -p "$SCRATCH_DIR/queue/missions/test-mission"
# review-plan.sh 本体・依存スクリプトは実ファイルをそのままコピーする
# (ロジックを書き換えずにテストするため)。
cp "$REAL_REPO/scripts/review-plan.sh" "$SCRATCH_DIR/scripts/"
cp "$REAL_REPO/scripts/lib_mux.sh" "$SCRATCH_DIR/scripts/"
cp "$REAL_REPO/scripts/lib_mux.py" "$SCRATCH_DIR/scripts/"
cp "$REAL_REPO/scripts/wait_for_plan_review.sh" "$SCRATCH_DIR/scripts/"
# t001 (mission 20260909-dead-config-sweep): review-plan.sh はモデル解決に
# scripts/lib_model.py と config/crewvia.yaml を必須で読むようになった。
mkdir -p "$SCRATCH_DIR/config"
cp "$REAL_REPO/scripts/lib_model.py" "$SCRATCH_DIR/scripts/"
cp "$REAL_REPO/config/crewvia.yaml" "$SCRATCH_DIR/config/"

RESULT_DIR="$(mktemp -d /tmp/crewvia-test-review-plan-result-XXXXXX)"

# claude CLI を実際には起動しないスタブに差し替える。スタブは:
#   - 受け取った AGENT_NAME (= review-plan.sh が export したもの) を記録
#   - その AGENT_NAME で実際に hooks/pre-tool-use.sh (本物) を呼び、
#     Bash tool の判定結果を記録
#   - wait_for_plan_review.sh のポーリングが即座に成功するよう、有効な
#     verdict を書いた plan_review.md を書いて終了
FAKE_BIN_DIR="$(mktemp -d /tmp/crewvia-test-review-plan-bin-XXXXXX)"
cat > "$FAKE_BIN_DIR/claude" << FAKESCRIPT
#!/usr/bin/env bash
echo "\${AGENT_NAME:-}" > "$RESULT_DIR/captured_agent_name.txt"
echo '$BASH_TOOL_JSON' \\
  | AGENT_NAME="\${AGENT_NAME:-}" SKILLS="\${SKILLS:-}" CREWVIA_REPO="$REAL_REPO" \\
    bash "$REAL_REPO/hooks/pre-tool-use.sh" \\
  > "$RESULT_DIR/hook_decision.json" 2>"$RESULT_DIR/hook_decision.stderr"
cat > "queue/missions/test-mission/plan_review.md" << 'EOF'
**Verdict:** approve

# Plan Review: test-mission (stub claude)
EOF
exit 0
FAKESCRIPT
chmod +x "$FAKE_BIN_DIR/claude"

# review-plan.sh は mux_available && mux_spawn を先に試す。CREWVIA_HERDR_SOCK
# はテスト専用の socket 上書き変数 (README/CLAUDE.md に明記) — 実在しない
# パスを指すことで、実運用の herdr サーバーに一切触れずに mux を
# "unavailable" 扱いにし、inline フォールバック経路 (実コード) を通す。
#
# AGENT_NAME="$DIRECTOR_NAME" が Director セッションからの継承を模す本体。
set +e
AGENT_NAME="$DIRECTOR_NAME" \
  CREWVIA_MUX=herdr \
  CREWVIA_HERDR_SOCK="/tmp/crewvia-test-nonexistent-socket-$$" \
  PATH="$FAKE_BIN_DIR:$PATH" \
  timeout 30 bash "$SCRATCH_DIR/scripts/review-plan.sh" test-mission \
  > "$RESULT_DIR/review_plan_stdout.log" 2> "$RESULT_DIR/review_plan_stderr.log"
REVIEW_RC=$?
set -e

if [[ "$REVIEW_RC" -ne 0 ]]; then
  echo "  (info) review-plan.sh exit=$REVIEW_RC — stderr:" >&2
  cat "$RESULT_DIR/review_plan_stderr.log" >&2
fi

CAPTURED_AGENT_NAME="$(cat "$RESULT_DIR/captured_agent_name.txt" 2>/dev/null || echo '<missing>')"
if [[ "$CAPTURED_AGENT_NAME" == "Plan-Reviewer" ]]; then
  pass "Director (AGENT_NAME=$DIRECTOR_NAME) から review-plan.sh を起動しても、reviewer 子プロセスの AGENT_NAME は 'Plan-Reviewer' に上書きされる"
else
  fail "reviewer 子プロセスの AGENT_NAME が 'Plan-Reviewer' になっていない — got '$CAPTURED_AGENT_NAME'"
fi

echo ""
echo "--- Test 3 (本修正・実運用経路): その 'Plan-Reviewer' env で hook を呼ぶと Bash が deny される (Director bypass を回避) ---"
if [[ -f "$RESULT_DIR/hook_decision.json" ]] && grep -q '"permissionDecision":"deny"' "$RESULT_DIR/hook_decision.json"; then
  pass "AGENT_NAME='Plan-Reviewer' では Bash が deny される — SKILLS=plan_review の Bash 全面禁止に実際に到達している"
else
  fail "Bash が deny されなかった (Director bypass が依然として発火している可能性) — content: $(cat "$RESULT_DIR/hook_decision.json" 2>/dev/null || echo '<missing>')"
fi

rm -rf "$RESULT_DIR" "$FAKE_BIN_DIR"

echo ""
echo "--- Test 4 (静的回帰ガード): review-plan.sh の mux 経路・inline フォールバック経路の両方に AGENT_NAME='Plan-Reviewer' の export があること ---"
COUNT="$(grep -c "export AGENT_NAME='Plan-Reviewer'" "$REAL_REPO/scripts/review-plan.sh")"
if [[ "$COUNT" -ge 2 ]]; then
  pass "両経路 (mux INLINE_CMD / inline フォールバック) に AGENT_NAME='Plan-Reviewer' の export がある (count=$COUNT)"
else
  fail "AGENT_NAME='Plan-Reviewer' の export が両経路分 (2箇所以上) 見つからない — count=$COUNT"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
