#!/usr/bin/env bash
# test_kai_review.sh — kai-review.sh (PR#180, R-1) の F1-F4 / F6 / R-1 回帰テスト
#
# 背景: scripts/kai-review.sh に対する Codex 自身の self-review findings
#   (F1/F1b/F2/F3/F4) と、Director 追加指示の F6 (--dry-run) を PR#180 で
#   修正した。fake CLI テストは応答形式のズレを隠しがち
#   (memory: fake-cli-and-qa-fail-gaps — 過去に fake CLI の bats が応答形式
#   バグを見逃した事例あり) なので、fake codex の出力 fixture は手書きの
#   想像ではなく、t001 で実測した codex-cli 0.144.5 (`codex exec review`) の
#   実出力をそのまま使う。fixture の出所は各 fixture の直前コメントに記載する。
#   （JSON forward-compat パスのみ、現行 CLI では再現できない仮想シナリオなので
#   意図的に合成した — 該当箇所に明記）
#
# R-1 (t001, mission 20260909-safety-gate-hardening): PR#180 の F-3 が導入した
# 「critical キーワード + 同一行否定語除外」の safety net は、critical な
# 指摘と無関係な否定語が同一行に同居すると健全な報告と誤判定し、本物の
# critical finding を見逃して auto-done してしまう欠陥 (R-1) を持っていた
# (これで「倒れる方向が自動承認」の欠陥は 4 回目)。R-1 でこの safety net を
# 全廃し、JSON findings 配列も [P#] タグも一切見つからない場合
# (HAD_SIGNAL=0) は内容に関わらず無条件で NEEDS-DIRECTOR相当 (fail-closed) に
# 倒すよう変更した。詳細は scripts/kai-review.sh のヘッダー / 判定ロジック
# コメントと本ファイル中の R-1 セクションを参照。
#
# 検証内容:
#   F2  findings 判定: [P#] タグ判定 (findings 空→done, P1 あり→needs-director) +
#       JSON forward-compat 判定
#   R-1 構造化シグナル無し (HAD_SIGNAL=0) の fail-closed 化: タグ無し・JSON
#       findings 配列無しの出力は内容 (critical キーワードの有無や文脈) に
#       関わらず一律 NEEDS-DIRECTOR相当になること
#   F3  並列衝突: kai-review.sh を 2 本同時実行し、出力ファイルが独立していること
#   F4  --mission forward: gh 失敗 / headRefName 空 / codex 失敗 / output file 未生成の
#       各 failure path で plan.sh needs-director に --mission が渡ること
#   F1/F1b: review 実行前後で主 working tree (fixture repo) の HEAD が不変なこと、
#       専用 review worktree が正しい commit を指しかつ後片付けされること
#   F6  --dry-run: plan.sh への書き込み (pull/done/needs-director) が一切発生しないこと
#
# 実機動作確認 (実際の gh/codex CLI) は t003 (QA) が担当する。ここは fake CLI で
# ロジックを網羅する側に徹する。R-1 の設計判断そのもの (codex exec review の
# --base と カスタム PROMPT が併用不可であること、--output-schema が review
# サブコマンドの出力書式を変えないこと) は t001 で実際の codex-cli 0.144.5 に
# 対して実機確認済み (Result 参照)。
#
# 実行: bash scripts/test_kai_review.sh
# 副作用: /tmp 配下に一時ディレクトリ (git fixture repo 含む) を作成し終了時に削除する

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KAI_REVIEW_SH="$OWN_CHECKOUT_ROOT/scripts/kai-review.sh"
REAL_PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST=""
cleanup() {
  if [[ -n "${TMPDIR_TEST:-}" && -d "$TMPDIR_TEST" ]]; then
    rm -rf "$TMPDIR_TEST"
  fi
}
trap cleanup EXIT

echo "== test_kai_review.sh (kai-review.sh F1-F4/F6 回帰テスト, PR#180) =="

# ---------------------------------------------------------------------------
# Setup 1: 一時 git fixture repo (主 working tree の代役)
#
# kai-review.sh は $CREWVIA_REPO_ROOT に対して git fetch / worktree add/remove
# を実行する。本物の crewvia リポジトリを汚さないよう、完全に独立した使い捨て
# git repo を用意し、これを CREWVIA_REPO_ROOT として渡す。
# ---------------------------------------------------------------------------
TMPDIR_TEST="/tmp/crewvia-test-kai-review-$$"
UPSTREAM="$TMPDIR_TEST/upstream"
FIXTURE_REPO="$TMPDIR_TEST/fixture-repo"
mkdir -p "$UPSTREAM"

git -C "$UPSTREAM" init -q -b main
git -C "$UPSTREAM" config user.email test@example.com
git -C "$UPSTREAM" config user.name "Test"
echo "hello" > "$UPSTREAM/README.md"
git -C "$UPSTREAM" add -A
git -C "$UPSTREAM" commit -q -m "init"
git -C "$UPSTREAM" checkout -q -b feature-branch
echo "feature change" >> "$UPSTREAM/README.md"
git -C "$UPSTREAM" commit -q -am "feature commit"
FEATURE_TIP="$(git -C "$UPSTREAM" rev-parse feature-branch)"

# F-2 (PR#180 QA fix): GitHub は PR が存在する限り refs/pull/<PR#>/head を必ず
# 公開する。ここでは refs/pull/1/head と refs/pull/2/head (F3 並列テストの 2 本目
# 用) を作ってから feature-branch そのものは削除し、「fork PR / マージ後に
# 削除済みの branch」を模す (実機再現: PR#179)。kai-review.sh がもう
# origin/<branch> 名を一切参照しないこと (常に refs/pull/<PR#>/head を fetch
# すること) を、この状態でレビューが成功することによって検証する。
git -C "$UPSTREAM" update-ref refs/pull/1/head feature-branch
git -C "$UPSTREAM" update-ref refs/pull/2/head feature-branch
git -C "$UPSTREAM" checkout -q main
git -C "$UPSTREAM" branch -D feature-branch >/dev/null

git clone -q "$UPSTREAM" "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" config user.email test@example.com
git -C "$FIXTURE_REPO" config user.name "Test"
MAIN_WT_HEAD_BEFORE_ALL="$(git -C "$FIXTURE_REPO" rev-parse HEAD)"

# plan.sh (bash 部分) は自分自身のファイル位置から REPO_ROOT を決め、
# `<REPO_ROOT>/scripts/git-helpers.sh` の有無で「pull 時に worktree を
# 自動作成するか」を判断する。本物の plan.sh をこの fixture repo にコピーし、
# git-helpers.sh は置かない → pull がこのテストの外側 (本物の crewvia repo) に
# 余計な worktree を作らないようにする。plan.sh 本体のロジックは本物のまま。
mkdir -p "$FIXTURE_REPO/scripts"
cp "$REAL_PLAN_SH" "$FIXTURE_REPO/scripts/plan.sh"

QUEUE="$FIXTURE_REPO/queue"
MISSION_SLUG="test-mission"
TASKS_DIR="$QUEUE/missions/$MISSION_SLUG/tasks"
mkdir -p "$TASKS_DIR" "$QUEUE/archive"
printf 'active_missions:\n  - %s\ndefault_mission: %s\n' "$MISSION_SLUG" "$MISSION_SLUG" > "$QUEUE/state.yaml"
printf 'title: Test Mission\nslug: %s\nstatus: in_progress\ncreated_at: 2026-09-08T00:00:00Z\ncompleted_at: null\nnext_task_id: 900\n' \
  "$MISSION_SLUG" > "$QUEUE/missions/$MISSION_SLUG/mission.yaml"

write_task() {
  local id="$1" title="$2"
  printf -- '---\nid: %s\ntitle: "%s"\nskills: [codex-review]\npriority: medium\nstatus: pending\nblocked_by: []\ntarget_dir: null\nworker: null\nstarted_at: null\ncompleted_at: null\npr_number: 1\n---\n\n## Description\n%s\n\n## Result\n' \
    "$id" "$title" "$title" > "$TASKS_DIR/${id}.md"
}

# --skip-pull は「task は既に in_progress である」ことを前提とする
# (kai-review.sh のヘッダーコメント参照)。plan.sh needs-director は
# 現在 status が in_progress でない task を die() で拒否するため、
# --skip-pull 経路の実行系テストでは in_progress で書き出す必要がある。
write_task_in_progress() {
  local id="$1" title="$2"
  printf -- '---\nid: %s\ntitle: "%s"\nskills: [codex-review]\npriority: medium\nstatus: in_progress\nblocked_by: []\ntarget_dir: null\nworker: Kai-codex-test\nstarted_at: "2026-09-08T00:00:00Z"\ncompleted_at: null\npr_number: 1\n---\n\n## Description\n%s\n\n## Result\n' \
    "$id" "$title" "$title" > "$TASKS_DIR/${id}.md"
}

task_status() {
  local id="$1"
  grep -m1 '^status:' "$TASKS_DIR/${id}.md" | awk '{print $2}'
}

task_sha() {
  local id="$1"
  sha256sum "$TASKS_DIR/${id}.md" | awk '{print $1}'
}

# ---------------------------------------------------------------------------
# Setup 2: fake gh / codex CLI (PATH 先頭に挿入)
# ---------------------------------------------------------------------------
FAKE_BIN_DIR="$TMPDIR_TEST/fake-bin"
mkdir -p "$FAKE_BIN_DIR"

cat > "$FAKE_BIN_DIR/gh" <<'EOS'
#!/usr/bin/env bash
# Fake gh CLI for kai-review.sh regression tests.
#   FAKE_GH_HEAD_BRANCH  - `gh pr view --json headRefName` が返す branch 名
#                          (未設定/空 = headRefName 空を再現)
#   FAKE_GH_FAIL=1       - `gh pr view` の失敗 (非ゼロ終了 + stderr) を再現
if [[ "${1:-}" == "pr" && "${2:-}" == "view" ]]; then
  if [[ "${FAKE_GH_FAIL:-0}" == "1" ]]; then
    echo "fake gh: GraphQL: could not resolve to a PullRequest" >&2
    exit 1
  fi
  echo "${FAKE_GH_HEAD_BRANCH:-}"
  exit 0
fi
echo "fake gh: unsupported args: $*" >&2
exit 1
EOS
chmod +x "$FAKE_BIN_DIR/gh"

cat > "$FAKE_BIN_DIR/codex" <<'EOS'
#!/usr/bin/env bash
# Fake codex CLI for kai-review.sh regression tests.
# 実際の呼び出し形式: codex exec -C <dir> review --base main [-m <model>] --ephemeral -o <file>
#   FAKE_CODEX_FIXTURE   - -o <file> にコピーするフィクスチャファイル
#   FAKE_CODEX_EXIT      - 終了コード (default 0)
#   FAKE_CODEX_NO_OUTPUT - "1" なら -o ファイルを書いた後に削除する
#                          (「codex は exit 0 だが出力ファイルが無い」を再現)
#   FAKE_CODEX_LOG       - 設定時、"<-C dir>|<-o file>|<-C dir の HEAD sha>" を追記する
#                          (F1/F3 の並列・isolation 検証用)
args=("$@")
cd_dir=""
out_file=""
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    -C) cd_dir="${args[$((i + 1))]}" ;;
    -o) out_file="${args[$((i + 1))]}" ;;
  esac
done

if [[ -n "${FAKE_CODEX_LOG:-}" ]]; then
  head_sha=""
  if [[ -n "$cd_dir" && -d "$cd_dir" ]]; then
    head_sha="$(git -C "$cd_dir" rev-parse HEAD 2>/dev/null || echo "?")"
  fi
  echo "${cd_dir}|${out_file}|${head_sha}" >> "$FAKE_CODEX_LOG"
fi

if [[ -n "$out_file" && -n "${FAKE_CODEX_FIXTURE:-}" ]]; then
  cp "$FAKE_CODEX_FIXTURE" "$out_file"
fi

if [[ "${FAKE_CODEX_NO_OUTPUT:-0}" == "1" && -n "$out_file" ]]; then
  rm -f "$out_file"
fi

exit "${FAKE_CODEX_EXIT:-0}"
EOS
chmod +x "$FAKE_BIN_DIR/codex"

run_kai() {
  PATH="$FAKE_BIN_DIR:$PATH" \
    CREWVIA_REPO_ROOT="$FIXTURE_REPO" \
    CREWVIA_QUEUE="$QUEUE" \
    AGENT_NAME="Kai-codex-test" \
    bash "$KAI_REVIEW_SH" "$@"
}

# ---------------------------------------------------------------------------
# Setup 3: fixtures (t001 で実測した codex-cli 0.144.5 の実出力をそのまま使用)
#
# 出所:
#   clean.txt        — 2026-09-08 t001 作業中、.crewvia-env のみの diff に対し
#                       `codex exec review --base main --ephemeral -o <file>`
#                       (codex-cli 0.144.5) を実行して得た生の出力そのもの。
#   p1_findings.txt  — 2026-09-08 t001 で PR#175 に対し
#                       `bash scripts/kai-review.sh --pr 175 --task t001 --dry-run`
#                       を実行した際に、実際の codex-cli 0.144.5 が書き出した
#                       出力ファイル ($OUTPUT_FILE) の内容そのもの。
#   json_*.txt / prose_critical_no_tag.txt / tag_and_critical_keyword.txt —
#                       現行 codex-cli (0.144.5) では再現できない仮想シナリオ
#                       (JSON forward-compat パス / critical キーワードの
#                       safety net・上書き非適用の確認) を検証するための
#                       意図的な合成 fixture。実測ではない。
#   p0_findings.txt   — 合成 fixture。実機で codex が [P0] を出す場面は
#                       t008 時点で未観測 (潜在バグ) だが、実際の出力書式
#                       (`- [P#] タイトル — file:line` + 本文) を模して
#                       F-1 (P0 抜けバグ) を検証する。
#   f3_repro_*.txt    — t008 (QA FAIL) の Description に記載された F-3 の
#                       再現例 3 件をそのまま fixture 化したもの (QA が実機で
#                       観測した誤発火パターン)。#   r1_repro_*.txt    (t001, R-1) t001 の Description に記載された再現例をそのまま
#                       fixture 化したもの。critical な指摘と無関係な否定語 (別の
#                       節にある "not"/"cannot" 等) が同一行に同居し、旧ロジック
#                       (同一行否定語除外) が健全な報告と誤判定して auto-done
#                       していた実例。
# ---------------------------------------------------------------------------
FIXTURES_DIR="$TMPDIR_TEST/fixtures"
mkdir -p "$FIXTURES_DIR"

cat > "$FIXTURES_DIR/clean.txt" <<'FIX'
The patch only updates the worktree-specific `.crewvia-env` identifiers, and the new values consistently match the current mission, task, and worktree slug. No functional regression is evident.
FIX

cat > "$FIXTURES_DIR/p1_findings.txt" <<'FIX'
The added operational guidance describes a workflow that cannot currently run because its entrypoint is absent, and its required task identifier conflicts with the instruction not to create a Kai task.

Full review comments:

- [P1] Add the referenced reviewer entrypoint — /tmp/kai-review-wt.TgSagx/skills/crewvia-plan-review/SKILL.md:208-209
  The documented workflow cannot be executed because `scripts/kai-review.sh` does not exist in this patch or elsewhere in the repository. Every recommended Kai invocation therefore fails immediately; include the script before directing Priya and Director to use it.

- [P2] Define the task ID required by the Kai command — /tmp/kai-review-wt.TgSagx/skills/crewvia-plan-review/SKILL.md:194-196
  In the two-reviewer scenario this explicitly creates only Seo's task, but the Kai command below requires `--task <id>` and is described as reporting through `plan.sh`. No Kai task ID is therefore available; using Seo's ID would overwrite or transition Seo's task instead of independently recording Kai's verdict. Define a non-dispatchable Kai task or change the helper/reporting contract so it does not require a task.
FIX

# 合成 fixture (現行 CLI では観測されない forward-compat / safety-net パス用)
cat > "$FIXTURES_DIR/json_empty.txt" <<'FIX'
{"findings":[],"overall_correctness":"patch is correct"}
FIX

cat > "$FIXTURES_DIR/json_p1.txt" <<'FIX'
{"findings":[{"title":"boom","body":"crashes on empty input","priority":1}],"overall_correctness":"patch has issues"}
FIX

cat > "$FIXTURES_DIR/prose_critical_no_tag.txt" <<'FIX'
This change introduces a critical security vulnerability in the auth flow.
FIX

# p0_findings.txt: 合成 fixture。t008 (QA FAIL 対応) の Description に記載の通り、
# 実機で codex が [P0] を出す場面は現時点で未観測 (潜在バグ) だが、タグ抽出の
# 正規表現が [P0] を拾い落とすと「危険な方向 (自動承認)」に倒れるため、実際の
# codex 出力書式 (`- [P#] タイトル — file:line` + 本文) を模して検証する。
cat > "$FIXTURES_DIR/p0_findings.txt" <<'FIX'
This introduces a release-blocking defect that must be caught before merge.

Review comment:

- [P0] Data loss on concurrent writes — scripts/example.sh:42
  Two workers writing to the same file without locking will silently corrupt state, losing previously written data. This must be fixed before merge.
FIX

# F-3 (QA FAIL 再現, PR#180): t008 Description に記載された 3 つの再現例そのもの。
# いずれも [P#] タグの無い「否定文脈の健全な報告文」であり、旧ロジックでは
# critical キーワード fallback がここに誤爆して needs-director になっていた。
cat > "$FIXTURES_DIR/f3_repro_no_critical_issues.txt" <<'FIX'
No critical issues found. The change looks good.
FIX

cat > "$FIXTURES_DIR/f3_repro_no_security_vuln.txt" <<'FIX'
This is a clean patch with no security vulnerability.
FIX

cat > "$FIXTURES_DIR/f3_repro_nothing_must_be_fixed.txt" <<'FIX'
Overall LGTM, nothing must be fixed.
FIX

# R-1 (t001) 再現例そのもの: t001 Description に記載された、Phase 3 の QA t010 が
# 実測した誤判定パターン。"critical" は本物の指摘だが、同一行にある否定語
# ("does not"/"cannot") は別の節 ("a workaround"/"recover") を否定しているだけで
# "critical" 自体を否定していない。旧ロジック (同一行否定語除外) はこの区別が
# できず、行ごと除外して安全側 (auto-done) に倒していた。
cat > "$FIXTURES_DIR/r1_repro_unrelated_negation.txt" <<'FIX'
This introduces a critical race condition in the queue writer that does not
have a workaround, and callers cannot recover once it triggers.
FIX

# [P3] タグのみの合成 fixture。「構造化シグナルがある場合の auto-done 経路」を
# 検証する実行系テスト (F2 実行系) で使う。critical という単語を本文に含むが
# タグ判定を信頼する (F-3 由来の確認観点を維持)。
cat > "$FIXTURES_DIR/tag_and_critical_keyword.txt" <<'FIX'
- [P3] Minor nit — foo.sh:1
  This is a very minor style nit and not a critical issue at all.
FIX

# ---------------------------------------------------------------------------
# F2 / R-1: findings 判定 (dry-run で判定結果のみ検証)
#
# R-1 (t001, mission 20260909-safety-gate-hardening) で、prose critical
# キーワード + 同一行否定語除外の safety net を全廃した。JSON findings 配列も
# [P#] タグも一切見つからない (HAD_SIGNAL=0) 場合は、内容に関わらず無条件で
# NEEDS-DIRECTOR相当 (fail-closed) に倒す。設計方針: 危険な結論 (自動 done) は
# allowlist (構造化シグナルによる確認が取れた場合のみ)、判定 unit は「JSON
# findings 配列」と「[P#] タグ」の 2 つに絞る。
#
# 実機検証 (t001, 2026-09-09, codex-cli 0.144.5): 当初案 (a)「codex に必ず
# 構造化タグを出力させるカスタム prompt を渡す」は、`codex exec review
# --base <BRANCH>` が `--base` と `[PROMPT]` (カスタム指示) の同時指定を
# CLI レベルで拒否する (`the argument '--base <BRANCH>' cannot be used with
# '[PROMPT]'`) ため実現不可能と判明した。`--output-schema` を付けても
# review サブコマンドの最終出力書式 (自然文 + [P#] タグ) は変化しないことも
# 実機で確認済み (空 diff / 実質的な diff の両方で検証)。現行 codex-cli は
# clean な review では常にタグ無しの自然文のみを返す (下記 clean.txt は
# その実測データ)。したがって本修正は、「clean review も含めタグの無い
# 出力は一律 needs-director に倒れる」という意図的なトレードオフである
# (詳細は Result 参照。将来的な改善余地: --commit や手動 diff 取得と
# --output-schema の組み合わせ)。
# ---------------------------------------------------------------------------
echo ""
echo "--- F2/R-1 [最重要]: [P#] タグ無し (実測 clean fixture) → NEEDS-DIRECTOR相当 (fail-closed。旧実装は誤って DONE にしていた) ---"
write_task t101 "F2 clean no signal"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/clean.txt" \
  run_kai --pr 1 --task t101 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "実測 clean fixture (タグ無し) → method=no-signal, NEEDS-DIRECTOR相当 (fail-closed, R-1 修正確認)"
else
  fail "REGRESSION (R-1): tag-less clean fixture should fail closed to NEEDS-DIRECTOR, not auto-done — rc=$rc out=$out"
fi

echo ""
echo "--- F2: [P#] タグ判定 — P1 finding あり (実測 fixture) → NEEDS-DIRECTOR相当 ---"
write_task t102 "F2 p1"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p1_findings.txt" \
  run_kai --pr 1 --task t102 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=tags needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "P1 findings fixture (実測, PR#175 dry-run で取得) → method=tags, NEEDS-DIRECTOR相当"
else
  fail "p1_findings fixture should judge as NEEDS-DIRECTOR via tags — rc=$rc out=$out"
fi

echo ""
echo "--- F2: JSON forward-compat — findings 空 → DONE 相当 (合成 fixture) ---"
write_task t103 "F2 json empty"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_empty.txt" \
  run_kai --pr 1 --task t103 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=0" && echo "$out" | grep -q "DONE/LGTM相当"; then
  pass "JSON findings=[] (合成) → method=json, DONE相当"
else
  fail "json_empty fixture should judge as DONE via json — rc=$rc out=$out"
fi

echo ""
echo "--- F2: JSON forward-compat — priority:1 finding あり → NEEDS-DIRECTOR相当 (合成 fixture) ---"
write_task t104 "F2 json p1"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_p1.txt" \
  run_kai --pr 1 --task t104 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "JSON priority:1 finding (合成) → method=json, NEEDS-DIRECTOR相当"
else
  fail "json_p1 fixture should judge as NEEDS-DIRECTOR via json — rc=$rc out=$out"
fi

echo ""
echo "--- R-1: 散文 critical キーワードあり・タグ無し → NEEDS-DIRECTOR相当 (fail-closed。旧 keyword safety net は廃止) ---"
write_task t105 "R-1 prose critical no tag"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/prose_critical_no_tag.txt" \
  run_kai --pr 1 --task t105 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "critical キーワードのみ (タグ無し) → method=no-signal, NEEDS-DIRECTOR相当 (fail-closed)"
else
  fail "prose_critical_no_tag fixture should judge as NEEDS-DIRECTOR — rc=$rc out=$out"
fi

echo ""
echo "--- R-1 [再現例そのもの]: critical + 無関係な否定語同居 → NEEDS-DIRECTOR相当 (旧実装は誤って auto-done していた) ---"
write_task t106 "R-1 repro unrelated negation"
out106=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/r1_repro_unrelated_negation.txt" \
  run_kai --pr 1 --task t106 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc106=0 || rc106=$?
if [[ $rc106 -eq 0 ]] && echo "$out106" | grep -q "method=no-signal needs_fix=1" && echo "$out106" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "R-1 再現例 (critical + 無関係な否定語同居) → NEEDS-DIRECTOR相当 (R-1 修正確認: 旧ロジックは同一行否定語除外で誤って auto-done していた)"
else
  fail "REGRESSION (R-1): critical finding with unrelated same-line negation should judge as NEEDS-DIRECTOR, not auto-done — rc=$rc106 out=$out106"
fi

# ---------------------------------------------------------------------------
# F2 (実行系): --dry-run を使わず、実際に plan.sh done / needs-director まで
# 走らせて task の status が正しく更新されることを確認する。
# ---------------------------------------------------------------------------
echo ""
echo "--- F2 (実行系): [P3] タグのみの findings → 実際に plan.sh done が呼ばれ status=done になる (構造化シグナルがある場合の auto-done 経路は健在) ---"
write_task_in_progress t110 "F2 real done via tag"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/tag_and_critical_keyword.txt" \
  run_kai --pr 1 --task t110 --mission "$MISSION_SLUG" --skip-pull > "$TMPDIR_TEST/t110.out" 2>&1
rc110=$?
st110="$(task_status t110)"
if [[ $rc110 -eq 0 && "$st110" == "done" ]] && grep -q "LGTM" "$TASKS_DIR/t110.md"; then
  pass "[P3] タグのみ → 実行後 status=done, Result に LGTM 記載 (構造化シグナルがある auto-done 経路は健在)"
else
  fail "[P3]-only tagged fixture real-run should set status=done — rc=$rc110 status=$st110 (see $TMPDIR_TEST/t110.out)"
fi

echo ""
echo "--- F2 (実行系): P1 findings fixture → 実際に plan.sh needs-director が呼ばれ status=needs_director になる ---"
write_task_in_progress t111 "F2 real needs-director"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p1_findings.txt" \
  run_kai --pr 1 --task t111 --mission "$MISSION_SLUG" --skip-pull > "$TMPDIR_TEST/t111.out" 2>&1
rc111=$?
st111="$(task_status t111)"
if [[ $rc111 -eq 0 && "$st111" == "needs_director" ]] && grep -q "NEEDS FIX" "$TASKS_DIR/t111.md"; then
  pass "P1 findings fixture → 実行後 status=needs_director, Result に NEEDS FIX 記載"
else
  fail "p1_findings real-run should set status=needs_director — rc=$rc111 status=$st111 (see $TMPDIR_TEST/t111.out)"
fi

echo ""
echo "--- F2 (実行系) [R-1]: タグ無しの clean fixture → 実際に plan.sh needs-director が呼ばれ status=needs_director になる (旧実装は誤って status=done にしていた) ---"
write_task_in_progress t112 "R-1 real needs-director on no-signal"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/clean.txt" \
  run_kai --pr 1 --task t112 --mission "$MISSION_SLUG" --skip-pull > "$TMPDIR_TEST/t112.out" 2>&1
rc112=$?
st112="$(task_status t112)"
if [[ $rc112 -eq 0 && "$st112" == "needs_director" ]] && grep -q "NEEDS FIX" "$TASKS_DIR/t112.md"; then
  pass "タグ無し clean fixture (実行系) → 実際に status=needs_director になる (R-1 修正確認: 旧実装は誤って done にしていた)"
else
  fail "REGRESSION (R-1): tag-less clean fixture real-run should set status=needs_director, not done — rc=$rc112 status=$st112 (see $TMPDIR_TEST/t112.out)"
fi

# ---------------------------------------------------------------------------
# F-1 (QA FAIL, PR#180): [P0] findings が LGTM 誤判定されていた欠陥の回帰テスト。
# レビューゲートが「危険な方向 (自動承認)」に倒れる欠陥だったため、これが最重要。
# ---------------------------------------------------------------------------
echo ""
echo "--- F-1 [最重要]: [P0] findings のみ → NEEDS-DIRECTOR相当になること (旧実装は自動 done していた) ---"
write_task t160 "F-1 p0 findings"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p0_findings.txt" \
  run_kai --pr 1 --task t160 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=tags needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "[P0] のみの finding → method=tags, NEEDS-DIRECTOR相当 (F-1 修正確認)"
else
  fail "REGRESSION (F-1): [P0]-only finding should judge as NEEDS-DIRECTOR, not auto-done — rc=$rc out=$out"
fi

echo ""
echo "--- F-1 (実行系): [P0] findings → 実際に plan.sh needs-director が呼ばれ status=needs_director になる ---"
write_task_in_progress t161 "F-1 p0 real needs-director"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p0_findings.txt" \
  run_kai --pr 1 --task t161 --mission "$MISSION_SLUG" --skip-pull > "$TMPDIR_TEST/t161.out" 2>&1
rc161=$?
st161="$(task_status t161)"
if [[ $rc161 -eq 0 && "$st161" == "needs_director" ]]; then
  pass "[P0] finding (実行系) → 実際に status=needs_director になる (旧実装なら誤って done になっていた)"
else
  fail "REGRESSION (F-1): [P0]-only finding real-run should set status=needs_director — rc=$rc161 status=$st161"
fi

echo ""
echo "--- F-1: JSON 経路でも priority:0 が needs-director になること ---"
cat > "$FIXTURES_DIR/json_p0.txt" <<'FIX'
{"findings":[{"title":"boom","body":"data loss on crash","priority":0}],"overall_correctness":"patch has issues"}
FIX
write_task t162 "F-1 json p0"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_p0.txt" \
  run_kai --pr 1 --task t162 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "JSON priority:0 finding → method=json, NEEDS-DIRECTOR相当 (F-1 JSON 経路修正確認)"
else
  fail "REGRESSION (F-1 JSON path): priority:0 finding should judge as NEEDS-DIRECTOR — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# [P1] (Seo review, PR#180 / t012): JSON 経路が「危険な値」の allowlist だった
# ため、列挙外の severity ("major"/"medium" 等) や priority/severity が丸ごと
# 欠損した finding が全て安全側 (自動 done) に落ちる fail-open 構造だった。
# denylist へ反転した修正の回帰テスト。Seo の実測値 (severity="major"/"medium"
# の組、severity/priority 欠損) をそのまま fixture 化している。
# ---------------------------------------------------------------------------
echo ""
echo "--- [P1]: 分類外 severity ('major'/'medium') → NEEDS-DIRECTOR相当 (旧実装は auto done, Seo実測値) ---"
cat > "$FIXTURES_DIR/json_unclassified_severity.txt" <<'FIX'
{"findings":[{"severity":"major","message":"a"},{"severity":"medium","message":"b"}],"overall_correctness":"patch has issues"}
FIX
write_task t163 "P1 json unclassified severity"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_unclassified_severity.txt" \
  run_kai --pr 1 --task t163 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "severity='major'/'medium' (denylist 外) → NEEDS-DIRECTOR相当 (P1 修正確認: allowlist→denylist 反転)"
else
  fail "REGRESSION (P1): unclassified severity ('major'/'medium') should judge as NEEDS-DIRECTOR, not auto-done — rc=$rc out=$out"
fi

echo ""
echo "--- [P1]: priority/severity 欠損の finding → NEEDS-DIRECTOR相当 (旧実装は auto done, Seo実測値) ---"
cat > "$FIXTURES_DIR/json_missing_classification.txt" <<'FIX'
{"findings":[{"message":"something is wrong here but no priority or severity field"}],"overall_correctness":"patch has issues"}
FIX
write_task t164 "P1 json missing classification"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_missing_classification.txt" \
  run_kai --pr 1 --task t164 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "priority/severity 欠損 → NEEDS-DIRECTOR相当 (P1 修正確認: 分類不能は危険側に倒す)"
else
  fail "REGRESSION (P1): finding with missing priority/severity should judge as NEEDS-DIRECTOR — rc=$rc out=$out"
fi

echo ""
echo "--- [P1]: 既知の安全値 (severity='low') は引き続き DONE相当 (denylist 反転後も安全値は通ること) ---"
cat > "$FIXTURES_DIR/json_low_severity.txt" <<'FIX'
{"findings":[{"severity":"low","message":"minor nit"}],"overall_correctness":"patch is correct"}
FIX
write_task t165 "P1 json low severity"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_low_severity.txt" \
  run_kai --pr 1 --task t165 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=0" && echo "$out" | grep -q "DONE/LGTM相当"; then
  pass "severity='low' (既知の安全値) → DONE相当 (denylist 反転後も安全値は正しく通る)"
else
  fail "REGRESSION (P1): known-safe severity='low' should still judge as DONE — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# [P2] (Seo 最終レビュー, PR#180 / t018): JSON 判定の「入口ゲート」自体が
# fail-open だった (同一構造の 3 度目: F-1 → [P1]/t012 → 今回)。
# `.findings` が存在しない/null の場合でも旧 `jq -e '.findings | length'` は
# exit 0 になり JSON 経路に入って自動 done + critical keyword safety net 無効化
# されていた。`.findings | arrays | length` へ変更し、`.findings` が真に配列で
# ある場合のみ JSON 経路に入るよう修正した回帰テスト。
# ---------------------------------------------------------------------------
echo ""
echo "--- [P2] t018: findings フィールド自体が欠損 → JSON 経路に入らず [P#] タグ判定へ fallback → 構造化シグナル無しで fail-closed (Seo実測値) ---"
cat > "$FIXTURES_DIR/json_missing_findings_field.txt" <<'FIX'
{"issues":[{"severity":"critical"}]}
FIX
write_task t190 "P2 t018 missing findings field"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_missing_findings_field.txt" \
  run_kai --pr 1 --task t190 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "findings フィールド欠損 (.issues のみ) → JSON 経路に入らず [P#] タグも無いため method=no-signal で fail-closed NEEDS-DIRECTOR相当 (P2/t018 + R-1 修正確認)"
else
  fail "REGRESSION (P2/t018): missing 'findings' field should NOT enter JSON path (fail-open gate) — rc=$rc out=$out"
fi

echo ""
echo "--- [P2] t018: findings が null → JSON 経路に入らず [P#] タグ判定へ fallback → 構造化シグナル無しで fail-closed (Seo実測値) ---"
cat > "$FIXTURES_DIR/json_null_findings.txt" <<'FIX'
{"findings":null,"summary":"3 critical bugs found"}
FIX
write_task t191 "P2 t018 null findings"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_null_findings.txt" \
  run_kai --pr 1 --task t191 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "findings: null → JSON 経路に入らず [P#] タグも無いため method=no-signal で fail-closed NEEDS-DIRECTOR相当 (P2/t018 + R-1 修正確認)"
else
  fail "REGRESSION (P2/t018): findings:null should NOT enter JSON path (fail-open gate) — rc=$rc out=$out"
fi

echo ""
echo "--- [P2] t018: findings が正当な空配列 → 引き続き JSON 経路で DONE相当 (ゲート修正で壊れていないこと) ---"
write_task t192 "P2 t018 legit empty findings array"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_empty.txt" \
  run_kai --pr 1 --task t192 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=0" && echo "$out" | grep -q "DONE/LGTM相当"; then
  pass "findings: [] (正当な空配列) → 引き続き method=json, DONE相当 (ゲート修正で壊れていない)"
else
  fail "REGRESSION (P2/t018): legit empty findings array should still enter JSON path and judge DONE — rc=$rc out=$out"
fi

echo ""
echo "--- [P2] t018: findings に未知 severity あり → 引き続き JSON 経路で NEEDS-DIRECTOR相当 (t012 の denylist が効き続けること) ---"
cat > "$FIXTURES_DIR/json_gate_and_denylist.txt" <<'FIX'
{"findings":[{"severity":"major"}]}
FIX
write_task t193 "P2 t018 gate plus denylist still works"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/json_gate_and_denylist.txt" \
  run_kai --pr 1 --task t193 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=json needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "findings に severity='major' のみ → 引き続き method=json, NEEDS-DIRECTOR相当 (ゲート修正後も t012 の denylist が効き続ける)"
else
  fail "REGRESSION (P2/t018): denylist inside the JSON path should still catch unknown severity after gate fix — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# [P2] (Seo review, PR#180 / t012): mktemp 導入後は `-f "$OUTPUT_FILE"` チェックが
# 到達不能になっていた (ファイルは常に存在するため)。`-s` (非空) 判定に変更した
# 修正の回帰テスト。codex が exit 0 で終わったのに出力が空のままのケースを検証。
# ---------------------------------------------------------------------------
echo ""
echo "--- [P2]: codex が exit 0 だが出力ファイルが空のまま → NEEDS-DIRECTOR相当 (旧 '-f' 判定では検出不能だった) ---"
: > "$FIXTURES_DIR/empty.txt"
write_task t166 "P2 empty output file"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/empty.txt" \
  run_kai --pr 1 --task t166 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] && echo "$out" | grep -q "produced no output"; then
  pass "codex exit 0 + 空ファイル → needs-director相当 (P2 修正確認: -s 判定で検出できる)"
else
  fail "REGRESSION (P2): empty (but existing) output file should be detected as failure — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# [P3] (Seo review, PR#180 / t012): call_needs_director の --dry-run ログ表示に、
# 実際の plan.sh 呼び出し (call_needs_director 末尾の非 dry-run 分岐) には
# 存在しない '--' 区切りが混ざっていた不整合の回帰テスト。この行は
# fail_needs_director 経由の failure path (F4 のケース群) で必ず通る。
# ---------------------------------------------------------------------------
echo ""
echo "--- [P3]: --dry-run の needs-director ログに実呼び出しに無い '--' が混ざらないこと (gh 失敗パスで確認) ---"
write_task t167 "P3 dry-run no stray dashes"
out=$(FAKE_GH_FAIL=1 run_kai --pr 1 --task t167 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] \
  && echo "$out" | grep -q "would call: plan.sh needs-director t167 --mission $MISSION_SLUG NEEDS FIX"; then
  pass "dry-run ログに実呼び出しと同じ引数列 (余計な '--' 無し) が表示される (P3 修正確認)"
else
  fail "REGRESSION (P3): dry-run log should not contain a stray '--' before the reason — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# F-2 (QA FAIL, PR#180): fork PR / マージ後に削除済みの branch を review できない
# 欠陥の回帰テスト。fixture repo の feature-branch は Setup 1 で既に削除済みで
# refs/pull/1/head 経由でしか到達できない状態になっている (実機再現: PR#179)。
# ---------------------------------------------------------------------------
echo ""
echo "--- F-2: origin に branch が存在しなくても refs/pull/<PR#>/head 経由でレビューできる ---"
# Setup 1 で `git -C "$UPSTREAM" branch -D feature-branch` 済み。
# origin/feature-branch が存在しないことをまず確認する (前提条件のセルフチェック)。
if git -C "$FIXTURE_REPO" rev-parse --verify -q "origin/feature-branch" >/dev/null 2>&1; then
  fail "test precondition broken: origin/feature-branch should NOT exist (branch was deleted in Setup 1)"
else
  pass "前提確認: origin/feature-branch は存在しない (branch 削除済みを確認)"
fi

write_task t170 "F-2 deleted branch"
# 検証対象は「refs/pull 経由の fetch/worktree が完走するか」であり判定内容ではない
# ため、fixture は構造化シグナルがあり判定が安定する tag_and_critical_keyword.txt
# を使う (R-1 以降、タグ無し clean.txt は fail-closed で NEEDS-DIRECTOR相当になる)。
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/tag_and_critical_keyword.txt" \
  run_kai --pr 1 --task t170 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "DONE/LGTM相当"; then
  pass "origin に branch が無くても refs/pull/1/head 経由でレビュー成功 (F-2 修正確認)"
else
  fail "REGRESSION (F-2): review should succeed via refs/pull/<PR#>/head even without origin branch — rc=$rc out=$out"
fi

echo ""
echo "--- F-2: refs/pull/<PR#>/head の fetch に使った一時 local ref が後片付けされる ---"
leftover_refs="$(git -C "$FIXTURE_REPO" for-each-ref refs/kai-review-fetch/ 2>/dev/null)"
if [[ -z "$leftover_refs" ]]; then
  pass "refs/kai-review-fetch/* に後片付け漏れなし"
else
  fail "leftover refs found under refs/kai-review-fetch/: $leftover_refs"
fi

# ---------------------------------------------------------------------------
# F-3 → R-1 (t001): PR#180 の F-3 は「critical キーワード fallback の同一行
# 否定語除外」で clean review の誤 needs-director を防いでいたが、この
# ヒューリスティクス自体が R-1 (無関係な否定語で本物の critical 指摘を
# 見逃す = 危険な方向への誤判定) の温床だったため、R-1 (t001) で
# fallback を全廃した。t008 の 3 つの再現例は「タグ無し」という点で
# 今や区別なく fail-closed の対象であり、いずれも NEEDS-DIRECTOR相当に
# 倒れることを確認する (F-3 時点の「DONE相当であるべき」という前提は
# R-1 の設計変更により意図的に覆された — 危険な結論=自動doneはallowlist
# である以上、構造化シグナルが無い限り安全側と確認できない)。
# ---------------------------------------------------------------------------
echo ""
echo "--- F-3→R-1: 'No critical issues found. The change looks good.' → NEEDS-DIRECTOR相当 (タグ無しは fail-closed。F-3 時点の DONE 判定は R-1 で意図的に変更) ---"
write_task t180 "F-3 repro 1 (R-1 fail-closed)"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/f3_repro_no_critical_issues.txt" \
  run_kai --pr 1 --task t180 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "'No critical issues found...' → method=no-signal, NEEDS-DIRECTOR相当 (R-1: タグ無し fail-closed)"
else
  fail "tag-less output should fail closed to NEEDS-DIRECTOR regardless of content — rc=$rc out=$out"
fi

echo ""
echo "--- F-3→R-1: 'This is a clean patch with no security vulnerability.' → NEEDS-DIRECTOR相当 ---"
write_task t181 "F-3 repro 2 (R-1 fail-closed)"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/f3_repro_no_security_vuln.txt" \
  run_kai --pr 1 --task t181 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "'...no security vulnerability.' → method=no-signal, NEEDS-DIRECTOR相当 (R-1: タグ無し fail-closed)"
else
  fail "tag-less output should fail closed to NEEDS-DIRECTOR regardless of content — rc=$rc out=$out"
fi

echo ""
echo "--- F-3→R-1: 'Overall LGTM, nothing must be fixed.' → NEEDS-DIRECTOR相当 ---"
write_task t182 "F-3 repro 3 (R-1 fail-closed)"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/f3_repro_nothing_must_be_fixed.txt" \
  run_kai --pr 1 --task t182 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "'...nothing must be fixed.' → method=no-signal, NEEDS-DIRECTOR相当 (R-1: タグ無し fail-closed)"
else
  fail "tag-less output should fail closed to NEEDS-DIRECTOR regardless of content — rc=$rc out=$out"
fi

echo ""
echo "--- R-1: 否定語を伴わない本物の critical キーワードも引き続き needs-director になる (fail-closed の健全性確認) ---"
write_task t183 "R-1 genuine critical no tag"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/prose_critical_no_tag.txt" \
  run_kai --pr 1 --task t183 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=no-signal needs_fix=1" && echo "$out" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "否定語なしの genuine critical キーワード (タグ無し) → 引き続き NEEDS-DIRECTOR相当"
else
  fail "genuine critical keyword (no tag) should still trigger NEEDS-DIRECTOR — rc=$rc out=$out"
fi

echo ""
echo "--- R-1: [P#] タグがあれば本文の critical という単語の有無に関わらずタグ判定を信頼する ---"
write_task t184 "R-1 tag trusted over prose content"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/tag_and_critical_keyword.txt" \
  run_kai --pr 1 --task t184 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q "method=tags needs_fix=0" && echo "$out" | grep -q "DONE/LGTM相当"; then
  pass "[P3] タグのみ (本文に critical という単語を含む) → タグ判定を信頼し DONE相当 (構造化シグナルがあれば prose 内容は見ない)"
else
  fail "when a tag is present, its priority should be trusted over prose content — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# F4: --mission forward — 各 failure path
# ---------------------------------------------------------------------------
echo ""
echo "--- F4: gh 失敗 (gh pr view) → needs-director に --mission が渡る ---"
write_task t120 "F4 gh fail"
out=$(FAKE_GH_FAIL=1 run_kai --pr 1 --task t120 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] && echo "$out" | grep -q "would call: plan.sh needs-director t120 --mission $MISSION_SLUG" && echo "$out" | grep -q "gh pr view"; then
  pass "gh 失敗 → --mission $MISSION_SLUG 付きで needs-director 相当を呼ぶ (exit=1)"
else
  fail "gh failure path should forward --mission — rc=$rc out=$out"
fi

echo ""
echo "--- F4: headRefName 空 → needs-director に --mission が渡る ---"
write_task t121 "F4 empty headref"
out=$(FAKE_GH_HEAD_BRANCH="" run_kai --pr 1 --task t121 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] && echo "$out" | grep -q "would call: plan.sh needs-director t121 --mission $MISSION_SLUG" && echo "$out" | grep -q "headRefName empty"; then
  pass "headRefName 空 → --mission $MISSION_SLUG 付きで needs-director 相当を呼ぶ (exit=1)"
else
  fail "empty headRefName path should forward --mission — rc=$rc out=$out"
fi

echo ""
echo "--- F4: codex 失敗 (exit != 0) → needs-director に --mission が渡る ---"
write_task t122 "F4 codex fail"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_EXIT=1 \
  run_kai --pr 1 --task t122 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] && echo "$out" | grep -q "would call: plan.sh needs-director t122 --mission $MISSION_SLUG" && echo "$out" | grep -q "CODEX FAILURE"; then
  pass "codex 失敗 → --mission $MISSION_SLUG 付きで needs-director 相当を呼ぶ (exit=1)"
else
  fail "codex failure path should forward --mission — rc=$rc out=$out"
fi

echo ""
echo "--- F4: output file 未生成 (codex は exit 0 だがファイルが無い) → needs-director に --mission が渡る ---"
write_task t123 "F4 no output"
out=$(FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_EXIT=0 FAKE_CODEX_NO_OUTPUT=1 \
  run_kai --pr 1 --task t123 --mission "$MISSION_SLUG" --dry-run 2>&1) && rc=0 || rc=$?
if [[ $rc -eq 1 ]] && echo "$out" | grep -q "would call: plan.sh needs-director t123 --mission $MISSION_SLUG" && echo "$out" | grep -q "produced no output"; then
  pass "output file 未生成 → --mission $MISSION_SLUG 付きで needs-director 相当を呼ぶ (exit=1)"
else
  fail "missing-output-file path should forward --mission — rc=$rc out=$out"
fi

# ---------------------------------------------------------------------------
# F1/F1b: 主 working tree の HEAD 不変 + review worktree の内容/後片付け
# ---------------------------------------------------------------------------
echo ""
echo "--- F1/F1b: review 実行後も主 WT (fixture repo) の HEAD が不変 ---"
head_before="$(git -C "$FIXTURE_REPO" rev-parse HEAD)"
wt_count_before="$(git -C "$FIXTURE_REPO" worktree list | wc -l)"
CODEX_LOG="$TMPDIR_TEST/f1_codex.log"
write_task t130 "F1 head unchanged"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/clean.txt" FAKE_CODEX_LOG="$CODEX_LOG" \
  run_kai --pr 1 --task t130 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/t130.out" 2>&1
rc130=$?
head_after="$(git -C "$FIXTURE_REPO" rev-parse HEAD)"
wt_count_after="$(git -C "$FIXTURE_REPO" worktree list | wc -l)"

if [[ $rc130 -eq 0 && "$head_before" == "$head_after" ]]; then
  pass "review 実行前後で主 WT の HEAD が一致 ($head_before)"
else
  fail "main WT HEAD changed! before=$head_before after=$head_after rc=$rc130 (see $TMPDIR_TEST/t130.out)"
fi

if [[ "$wt_count_before" == "$wt_count_after" ]]; then
  pass "review 用 worktree が後片付けされ、worktree list の件数が実行前後で一致 ($wt_count_before)"
else
  fail "worktree leftover detected: before=$wt_count_before after=$wt_count_after entries"
fi

echo ""
echo "--- F1: review worktree は正しい commit (origin/feature-branch の tip) を指していた ---"
log_line="$(cat "$CODEX_LOG" 2>/dev/null)"
reviewed_dir="$(echo "$log_line" | cut -d'|' -f1)"
reviewed_head="$(echo "$log_line" | cut -d'|' -f3)"
if [[ "$reviewed_head" == "$FEATURE_TIP" ]]; then
  pass "codex に渡された -C ディレクトリの HEAD が feature-branch の tip ($FEATURE_TIP) と一致"
else
  fail "reviewed HEAD mismatch: expected=$FEATURE_TIP got=$reviewed_head (log: $log_line)"
fi

if [[ -n "$reviewed_dir" && ! -d "$reviewed_dir" ]]; then
  pass "review worktree ディレクトリ ($reviewed_dir) は実行後に削除されている"
else
  fail "review worktree directory should be removed after run — dir=$reviewed_dir"
fi

# ---------------------------------------------------------------------------
# F3: 並列衝突 — 2 本同時実行しても互いの出力ファイルを読まない
# ---------------------------------------------------------------------------
echo ""
echo "--- F3: kai-review.sh を 2 本同時実行しても出力ファイルが独立している ---"
write_task t140 "F3 parallel tagged done"
write_task t141 "F3 parallel p1"
LOG_A="$TMPDIR_TEST/f3_a.log"
LOG_B="$TMPDIR_TEST/f3_b.log"

(
  # R-1 以降、タグ無し clean 出力は fail-closed で NEEDS-DIRECTOR相当になるため、
  # このテストの「DONE 側」は [P#] タグ付きの構造化シグナルがある fixture を使う。
  FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/tag_and_critical_keyword.txt" FAKE_CODEX_LOG="$LOG_A" \
    run_kai --pr 1 --task t140 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/f3_a.out" 2>&1
  echo $? > "$TMPDIR_TEST/f3_a.rc"
) &
pid_a=$!

(
  FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p1_findings.txt" FAKE_CODEX_LOG="$LOG_B" \
    run_kai --pr 2 --task t141 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/f3_b.out" 2>&1
  echo $? > "$TMPDIR_TEST/f3_b.rc"
) &
pid_b=$!

wait "$pid_a" "$pid_b"

rc_a="$(cat "$TMPDIR_TEST/f3_a.rc" 2>/dev/null || echo "?")"
rc_b="$(cat "$TMPDIR_TEST/f3_b.rc" 2>/dev/null || echo "?")"
out_a="$(cat "$TMPDIR_TEST/f3_a.out" 2>/dev/null)"
out_b="$(cat "$TMPDIR_TEST/f3_b.out" 2>/dev/null)"
outfile_a="$(cut -d'|' -f2 "$LOG_A" 2>/dev/null)"
outfile_b="$(cut -d'|' -f2 "$LOG_B" 2>/dev/null)"

if [[ "$rc_a" == "0" && "$rc_b" == "0" ]] \
  && echo "$out_a" | grep -q "DONE/LGTM相当" \
  && echo "$out_b" | grep -q "NEEDS-DIRECTOR相当"; then
  pass "並列実行しても各プロセスが自分の fixture 通りの verdict を出す (clean→DONE, p1→NEEDS-DIRECTOR)"
else
  fail "parallel runs produced wrong verdicts — rc_a=$rc_a rc_b=$rc_b out_a=$out_a out_b=$out_b"
fi

if [[ -n "$outfile_a" && -n "$outfile_b" && "$outfile_a" != "$outfile_b" ]]; then
  pass "並列 2 プロセスの出力ファイルパスが異なる (mktemp による衝突回避を確認): $outfile_a != $outfile_b"
else
  fail "output files should differ between parallel runs — a=$outfile_a b=$outfile_b"
fi

# ---------------------------------------------------------------------------
# F6: --dry-run は plan.sh へ一切書き込まない
# ---------------------------------------------------------------------------
echo ""
echo "--- F6: --dry-run は plan.sh pull を呼ばない (status が pending のまま) ---"
write_task t150 "F6 dry-run no pull"
sha_before="$(task_sha t150)"
status_before="$(task_status t150)"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p1_findings.txt" \
  run_kai --pr 1 --task t150 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/t150.out" 2>&1
sha_after="$(task_sha t150)"
status_after="$(task_status t150)"
if [[ "$sha_before" == "$sha_after" && "$status_before" == "pending" && "$status_after" == "pending" ]]; then
  pass "--dry-run 実行後も task ファイルが完全に不変 (status=pending のまま, sha256 一致)"
else
  fail "--dry-run should not touch the task file — before=$status_before/$sha_before after=$status_after/$sha_after"
fi

echo ""
echo "--- F6: --dry-run は NEEDS-FIX 判定でも plan.sh needs-director を呼ばない ---"
write_task t151 "F6 dry-run no needs-director write"
sha_before2="$(task_sha t151)"
FAKE_GH_HEAD_BRANCH="feature-branch" FAKE_CODEX_FIXTURE="$FIXTURES_DIR/p1_findings.txt" \
  run_kai --pr 1 --task t151 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/t151.out" 2>&1
rc151=$?
sha_after2="$(task_sha t151)"
if [[ $rc151 -eq 0 && "$sha_before2" == "$sha_after2" ]]; then
  pass "P1 findings (dry-run) でも task ファイルは不変 (needs-director を実呼び出ししない)"
else
  fail "dry-run with P1 findings mutated task file — rc=$rc151 before=$sha_before2 after=$sha_after2"
fi

echo ""
echo "--- F6: --dry-run は failure path (gh 失敗) でも plan.sh needs-director を呼ばない ---"
write_task t152 "F6 dry-run failure path no write"
sha_before3="$(task_sha t152)"
FAKE_GH_FAIL=1 run_kai --pr 1 --task t152 --mission "$MISSION_SLUG" --dry-run > "$TMPDIR_TEST/t152.out" 2>&1
rc152=$?
sha_after3="$(task_sha t152)"
if [[ $rc152 -eq 1 && "$sha_before3" == "$sha_after3" ]]; then
  pass "gh 失敗 + --dry-run でも task ファイルは不変 (exit=1 は維持)"
else
  fail "dry-run failure path mutated task file — rc=$rc152 before=$sha_before3 after=$sha_after3"
fi

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
