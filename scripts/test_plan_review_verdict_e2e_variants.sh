#!/usr/bin/env bash
# scripts/test_plan_review_verdict_e2e_variants.sh
# t010 (mission 20260909-dead-config-sweep, QA t008 FINDING-1/2/3) の
# エンドツーエンド回帰テスト。
#
# 背景: QA (t008) は「reviewer が approve を一度も選んでいないのに mission
# が status: ready / last_verdict: approve になる経路」を、実 scripts/plan.sh
# review をエンドツーエンドで走らせて3つ実測した (FINDING-1/2/3。詳細は
# queue/missions/20260909-dead-config-sweep/tasks/t008.md の Result 参照)。
# scripts/lib_verdict.py の単体テスト (test_lib_verdict.sh) だけでは
# 「plan.sh review が実際にどう振る舞うか」(lint → reviewing → review-plan.sh
# → verdict 抽出 → rollback/cycle refund → mission 更新) までは検証できない
# ため、本ファイルは QA t008 の検証方法をそのまま恒久テスト化する。
#
# テスト方法: scripts/test_plan_review_cycle_refund.sh と同じ手法 — 実物の
# scripts/plan.sh を CREWVIA_QUEUE=<scratch> で実行し、scratch 側の
# scripts/review-plan.sh だけを差し替える。差し替え版は claude を一切
# 起動せず、plan_review.md を指定内容で書いた後、**本物の**
# scripts/wait_for_plan_review.sh (内部で本物の lib_verdict.py を呼ぶ。
# F1, mission 20260912-verdict-ci-launcher で
# normalize_plan_review_verdict.py は削除済み) を実行し、その OK/TIMEOUT
# 判定を review-plan.sh の終了コード規約 (OK なら0、それ以外1) として
# 再現する。plan.sh 側の verdict 抽出・rollback・cycle refund・mission 更新は
# すべて本物のコードパスを通る。claude CLI は一切起動しない。
#
# t015 (mission 20260912-verdict-ci-launcher, Director 設計判断1) 追記:
# 本物の scripts/plan.sh はもう plan_review.md を独立に読み直さない —
# review-plan.sh が書く queue/missions/<slug>/plan_review.verdict だけを
# 消費する (QA t003 Finn 実測 FAIL-1 の TOCTOU 対策)。この差し替え版
# review-plan.sh スタブは「本物の review-plan.sh が exit 0 する条件で
# plan_review.verdict も書く」という新しい契約を模す (実際の判定計算
# 自体は本物の scripts/lib_verdict.py を呼んで行うため、フェンス/否定形
# 等の判定ロジックは一切変えていない — plan.sh 側が正しく
# plan_review.verdict を信頼・拒否することを検証するのがこのテストの目的)。
#
# 実行: bash scripts/test_plan_review_verdict_e2e_variants.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWN_CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLAN_SH="$OWN_CHECKOUT_ROOT/scripts/plan.sh"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

_cleanup_dir() {
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$1" 2>/dev/null || true
}

echo "== test_plan_review_verdict_e2e_variants.sh (t010, QA t008 FINDING-1/2/3) =="

# $1 = variant名, $2 = plan_review.md に書く本文 ("__NO_FILE__" ならファイル
# を一切作らない = missing_file 相当), $3 = 期待する status
# (ready|drafting), $4 = 期待する last_verdict ("" なら null 期待), $5 = label
_run_variant() {
  local name="$1" body="$2" expect_status="$3" expect_verdict="$4" label="$5"
  local T="/tmp/crewvia-test-verdict-e2e-$$-${name}"
  _cleanup_dir "$T"
  mkdir -p "$T/scripts" "$T/config"
  export CREWVIA_QUEUE="$T/queue"
  unset TASKVIA_URL TASKVIA_TOKEN 2>/dev/null || true
  cp "$OWN_CHECKOUT_ROOT/scripts/lint_plan.py" "$T/scripts/"
  cp "$OWN_CHECKOUT_ROOT/scripts/lib_verdict.py" "$T/scripts/"
  cp -r "$OWN_CHECKOUT_ROOT/config/." "$T/config/"
  "$PLAN_SH" init "Test Mission" --mission testmission >/dev/null 2>&1

  local write_file="true"
  [[ "$body" == "__NO_FILE__" ]] && write_file="false"

  cat > "$T/scripts/review-plan.sh" << EOF
#!/usr/bin/env bash
set -uo pipefail
SLUG="\$1"
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
MISSION_DIR="\$(cd "\$SCRIPT_DIR/.." && pwd)/queue/missions/\$SLUG"
REVIEW_OUTPUT="\$MISSION_DIR/plan_review.md"
VERDICT_FILE="\$MISSION_DIR/plan_review.verdict"
rm -f "\$REVIEW_OUTPUT" "\$VERDICT_FILE"
START=\$(date +%s)
if [[ "$write_file" == "true" ]]; then
cat > "\$REVIEW_OUTPUT" << 'INNER'
$body
INNER
fi
WAIT_OUTPUT="\$(bash "$OWN_CHECKOUT_ROOT/scripts/wait_for_plan_review.sh" "\$REVIEW_OUTPUT" "\$START" 2 1)"
WAIT_RC=\$?
WAIT_STATUS="\$(printf '%s\n' "\$WAIT_OUTPUT" | head -1)"
if [[ "\$WAIT_RC" -eq 0 && "\$WAIT_STATUS" == "OK" ]]; then
  # t015 (Director 設計判断1): 本物の review-plan.sh と同じ契約 —
  # 判定計算 (lib_verdict.py) はここで実行し、確定した値だけを
  # plan_review.verdict にアトミックに書く。plan.sh はこれだけを読む。
  VERDICT="\$(python3 "\$SCRIPT_DIR/lib_verdict.py" "\$REVIEW_OUTPUT" 2>/dev/null || true)"
  if [[ -n "\$VERDICT" ]]; then
    # t018: plan.sh が渡す実行ごとの識別子 (CREWVIA_PLAN_REVIEW_RUN_ID) を
    # 2 行目に書く (本物の review-plan.sh と同じ形式。plan.sh は一致しなければ消費しない)。
    printf '%s\nrun_id=%s\n' "\$VERDICT" "\${CREWVIA_PLAN_REVIEW_RUN_ID:-}" > "\${VERDICT_FILE}.tmp" && mv "\${VERDICT_FILE}.tmp" "\$VERDICT_FILE"
    exit 0
  fi
fi
exit 1
EOF
  chmod +x "$T/scripts/review-plan.sh"

  set +e
  OUT="$("$PLAN_SH" review testmission 2>&1)"
  set -e
  local status verdict
  status="$(awk '/^status:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"
  verdict="$(awk '/^  last_verdict:/ { print $2; exit }' "$T/queue/missions/testmission/mission.yaml" 2>/dev/null)"

  local status_ok="false" verdict_ok="false"
  [[ "$status" == "$expect_status" ]] && status_ok="true"
  if [[ -z "$expect_verdict" ]]; then
    [[ "$verdict" == "null" || -z "$verdict" ]] && verdict_ok="true"
  else
    [[ "$verdict" == "$expect_verdict" ]] && verdict_ok="true"
  fi

  if [[ "$status_ok" == "true" && "$verdict_ok" == "true" ]]; then
    pass "$label — status=$status verdict=$verdict"
  else
    fail "$label — expected status=$expect_status verdict=${expect_verdict:-null}, got status=$status verdict=$verdict (output: $OUT)"
  fi
  _cleanup_dir "$T"
}

echo ""
echo "--- 正常系 (regression: approve/revise/reject は従来どおり動く) ---"
_run_variant "canon_approve" '**Verdict:** approve' "ready" "approve" \
  "規定形式 approve → status: ready, verdict: approve"
_run_variant "canon_revise" '**Verdict:** revise' "drafting" "revise" \
  "規定形式 revise → status: drafting, verdict: revise"
_run_variant "canon_reject" '**Verdict:** reject' "drafting" "reject" \
  "規定形式 reject → status: drafting, verdict: reject"

echo ""
echo "--- F1 (t002, mission 20260912-verdict-ci-launcher): 挙動変更 — 別表記の救済経路 (normalize_plan_review_verdict.py) を削除した ---"
_run_variant "go_normalized" '## 総合判定: **GO**' "drafting" "" \
  "別表記 (## 総合判定: **GO**) → もう正規化されず drafting (find_alt_verdict 削除。挙動変更、安全側)。本番は review-plan.sh の構造化出力経路が回収する"
_run_variant "blockquote_quoted_go" '# Plan Review (cycle 2)

前回のレビュー:

> ## 総合判定: **GO**

今回は判断を保留します。追加情報待ち。' "drafting" "" \
  "F1 blockquote (前 cycle の判定を引用): 旧 find_alt_verdict は blockquote を知らず誤 approve していた (7642f9b で実測再現済み) → drafting (削除後は安全側)"

echo ""
echo "--- FINDING-1: コードフェンス内にだけ判定がある → 誤 approve にならない ---"
_run_variant "fence_only" '見本:

```
**Verdict:** approve
```

実際の判定はまだ書いていません。' "drafting" "" \
  "フェンス内だけの approve → status: drafting (誤 approve だった旧挙動の修正確認)"

echo ""
echo "--- FINDING-2 [最重要]: フェンス内 approve が本物の判定として採用されない ---"
# t010 版はここで「フェンスを除去して本文末尾の revise を採用する」ことを
# 期待していた。t012 で判定を読む場所を「ファイルの最初の非空行」1点に
# 固定したため、この入力は 1 行目が判定行でなく **判定不能** になる
# (誤 approve にならないという要件は満たしたまま、フェンス記法の解釈を
#  やめた分だけ厳しい側に倒れる)。本番では review-plan.sh が構造化出力から
# verdict を回収して 1 行目に書き戻すため、reviewer の本当の判定は失われない。
_run_variant "mixed_fence" '書式例:

```
**Verdict:** approve
```

実際の判定は以下です。

**Verdict:** revise' "drafting" "" \
  "フェンス内approve + 本文末尾のrevise → 誤 approve にならず判定不能 (status: drafting)"

echo ""
echo "--- FINDING-3: テンプレート行をそのまま貼ると誤 approve にならない ---"
_run_variant "template_line" '**Verdict:** approve | revise | reject

# Plan Review: test' "drafting" "" \
  "未編集のテンプレート行 (approve | revise | reject) → status: drafting (旧挙動は誤って approve だった)"

echo ""
echo "--- t012 (QA t011): 実測された危険側入力が end-to-end で誤 approve にならないこと ---"
# t011 は lib_verdict 単体 8 件を実 plan.sh review に流し、全件が
# 「reviewer が approve を選んでいないのに status: ready / verdict: approve」
# に到達することを実測した。その 8 件 + 既存穴 1 件をそのまま恒久テスト化する。
_run_variant "not_approve" '**Verdict:** not approve' "drafting" "" \
  "値の否定形 (not approve) → drafting (t011 NEW-1)"
_run_variant "pending_no_approve" '**Verdict:** pending — do not approve yet' "drafting" "" \
  "値の否定形 (do not approve yet) → drafting (t011 NEW-1)"
_run_variant "cannot_approve_ja" '**Verdict:** approve できません' "drafting" "" \
  "値の否定形 (approve できません) → drafting (t011 既存穴)"
_run_variant "upper_approve" '**Verdict:** APPROVE' "drafting" "" \
  "大文字 APPROVE → drafting (t011 NEW-3: 危険側への緩和を戻した)"
_run_variant "odd_fence_mispair" 'A

```
**Verdict:** revise
```

```
**Verdict:** approve' "drafting" "" \
  "フェンスが奇数個 (旧実装はペア位置ずれで本物の revise を消していた) → drafting (t011 NEW-2)"
_run_variant "nested_fence" '````

```
**Verdict:** approve
```

````' "drafting" "" \
  "入れ子フェンス → drafting (t011 未閉塞だった既存穴)"
_run_variant "tilde_fence_only" '~~~
**Verdict:** approve
~~~' "drafting" "" \
  "~~~ フェンス → drafting (t011 未閉塞だった既存穴)"
_run_variant "unclosed_fence" '```
**Verdict:** approve' "drafting" "" \
  "閉じ忘れフェンス → drafting (t011 未閉塞だった既存穴)"
_run_variant "html_comment_ml" '<!--
**Verdict:** approve
-->' "drafting" "" \
  "複数行 HTML コメント内の判定 → drafting (t011 未閉塞だった既存穴)"

echo ""
echo "--- t012: 判定を読む場所は1行目のみ / 自己矛盾は判定不能 ---"
_run_variant "title_first" '# Plan Review: test

**Verdict:** approve' "drafting" "" \
  "タイトル行が先にある → 1行目が判定行でないため drafting (不変条件2)"
_run_variant "approve_with_comment" '**Verdict:** approve (軽微な指摘はあるが問題なし)' "drafting" "" \
  "値に註釈が付く → 完全一致しないため drafting (不変条件1。本番は構造化出力が回収する)"
_run_variant "self_contradiction" '**Verdict:** approve

note

**Verdict:** revise' "drafting" "" \
  "1行目 approve + 本文に revise → 自己矛盾として drafting"
# t018 (mission 20260912-verdict-ci-launcher, Director 設計判断2) で挙動変更:
# 以前は「同じ値の重複は矛盾ではない」として ready/approve だったが、verdict 行の
# 兆候が 2 つ以上あるファイルは値によらず書式違反として扱う。値が同じかどうかを
# 見ると、本文中の verdict 行の「値を読む」経路がもう 1 つ増え、1 行目 revise +
# 本文 approve (QA t016 B_k1) と同じ形の読み違いの余地が残るため。
_run_variant "dup_same_approve" '**Verdict:** approve

note

**Verdict:** approve' "drafting" "" \
  "同じ approve の重複 → t018 で書式違反 (兆候が 2 行) として drafting (挙動変更、安全側)"

echo ""
echo "--- 判定不能の一般化 (regression なし) ---"
_run_variant "no_verdict" '# body only, no verdict language' "drafting" "" \
  "判定語なし → status: drafting"
_run_variant "empty_file" '' "drafting" "" \
  "空ファイル → status: drafting"
_run_variant "missing_file" "__NO_FILE__" "drafting" "" \
  "plan_review.md が作られない → status: drafting"
_run_variant "unknown_word" '**Verdict:** STOP' "drafting" "" \
  "未知の判定語 (STOP) → status: drafting"
_run_variant "negated_go" '## 総合判定: GO は出せない' "drafting" "" \
  "否定形 (GO は出せない) → status: drafting"

unset CREWVIA_QUEUE

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
