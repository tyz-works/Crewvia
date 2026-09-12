#!/usr/bin/env bash
# scripts/test_lib_verdict.sh
# scripts/lib_verdict.py の回帰テスト
# (t010 で新規作成 / t012 で不変条件の変更に合わせて全面改訂,
#  mission 20260909-dead-config-sweep, QA t008 FINDING-1/2/3 → QA t011 NEW-1/2)。
#
# 背景: t010 版の lib_verdict は「フェンスを除去 → ファイル全体から
# `**Verdict:**` 行を集める → 行の値に含まれる判定語を数える」形だった。
# QA t011 はこの形に対し新規 fail-open を 3 件実測した
# (`**Verdict:** not approve` が approve になる / フェンス除去正規表現の
#  ペア位置ずれで本物の revise が消える 等)。t012 で以下の不変条件に
# 作り直した:
#
#   1. 値は完全一致 allowlist (approve/revise/reject の1語ちょうど、小文字)
#   2. 判定を読む場所はファイルの最初の非空行のみ (全体走査で採らない)
#   3. それ以外はすべて判定不能 (approve に倒れる分岐を持たない)
#   4. フェンス/HTML コメントの除去ヒューリスティクスを使わない
#      (読む場所を1点に固定することで、除去自体を不要にする)
#
# 追加の拒否専用スキャン: 1行目と異なる値の完全一致 verdict 行が本文に
# あれば判定不能に落とす (結果を None 方向にしか動かさないため不変条件2を
# 損なわない)。
#
# 検証内容:
#   A. 正常系 (canon approve/revise/reject) に regression がないこと
#   B. QA t011 が実測した危険側入力 8 件 + 既存穴が全て判定不能になること
#      (not_approve / pending_no_approve / odd_fence_mispair / upper_approve /
#       nested_fence / tilde_fence_only / unclosed_fence / html_comment_ml /
#       cannot_approve_ja)
#   C. フェンス記法のバリエーション (``` / ~~~ / 入れ子 / 閉じ忘れ /
#      インデント)、値の否定形、HTML コメント — t011 が
#      「著者テストに1件も含まれない」と指摘した種類を必ず含める
#   D. 判定を書く場所が1行目でない場合は判定不能になること (不変条件2)
#   E. 自己矛盾 (異なる値の verdict 行が併存) は判定不能、同一値の重複は許容
#
# 実行: bash scripts/test_lib_verdict.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/lib_verdict.py"

PASS_COUNT=0
FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "  PASS: $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo "  FAIL: $1"; }

TMPDIR_TEST="/tmp/crewvia-test-lib-verdict-$$"
mkdir -p "$TMPDIR_TEST"
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$TMPDIR_TEST"' EXIT

echo "== test_lib_verdict.sh (t010 → t012 全面改訂) =="

# $1 = ファイル内容, $2 = 期待する標準出力 ("" なら判定不能を期待),
# $3 = ラベル
_check() {
  local content="$1" expected="$2" label="$3"
  local f="$TMPDIR_TEST/case.md"
  printf '%s' "$content" > "$f"
  local out rc
  out="$(python3 "$LIB" "$f" 2>/dev/null)"
  rc=$?
  if [[ -z "$expected" ]]; then
    if [[ "$rc" -ne 0 && -z "$out" ]]; then
      pass "$label → 判定不能 (期待どおり)"
    else
      fail "$label → 判定不能であるべきなのに rc=$rc out='$out'"
    fi
  else
    if [[ "$rc" -eq 0 && "$out" == "$expected" ]]; then
      pass "$label → '$expected' (期待どおり)"
    else
      fail "$label → '$expected' を期待したが rc=$rc out='$out'"
    fi
  fi
}

echo ""
echo "--- A: 正常系 (regression) ---"
_check '**Verdict:** approve' "approve" "canon approve"
_check '**Verdict:** revise' "revise" "canon revise"
_check '**Verdict:** reject' "reject" "canon reject"
_check $'\n\n**Verdict:** approve\n\n## Summary\n問題なし。' "approve" "先頭に空行、1行目が判定 + 後続本文あり"
_check $'**Verdict:** approve\n\nnote\n\n**Verdict:** approve' "approve" "同じ approve が複数行 → approve のまま (過剰な安全側倒れをしない)"
_check $'**Verdict:**\tapprove  ' "approve" "値の前後の空白/タブのみ → 許容"

echo ""
echo "--- B: QA t011 が実測した危険側入力 (すべて判定不能でなければならない) ---"
# NEW-1 系: 値の部分一致で否定形が approve になっていた
_check '**Verdict:** not approve' "" "not approve (t011 NEW-1)"
_check '**Verdict:** pending — do not approve yet' "" "pending — do not approve yet (t011 NEW-1)"
_check '**Verdict:** approve できません' "" "approve できません (t011 既存穴 cannot_approve_ja)"
_check '**Verdict:** approved' "" "approved (過去形 — 完全一致しない)"
# NEW-3 系: 大文字小文字の緩和を戻す
_check '**Verdict:** APPROVE' "" "APPROVE (t011 NEW-3: 危険側への緩和を戻す)"
_check '**Verdict:** Approve' "" "Approve (同上)"
# NEW-2 系: フェンス除去のペア位置ずれ
_check $'A\n\x60\x60\x60\n**Verdict:** revise\n\x60\x60\x60\n\x60\x60\x60\n**Verdict:** approve' "" \
  "3連バッククォートが奇数個 (ペア位置ずれで revise が消え approve が残っていた, t011 NEW-2)"
# 未閉塞だった既存穴 4 件
_check $'\x60\x60\x60\x60\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\x60\x60\x60\x60' "" "入れ子フェンス (t011 nested_fence)"
_check $'~~~\n**Verdict:** approve\n~~~' "" "~~~ フェンス (t011 tilde_fence_only)"
_check $'\x60\x60\x60\n**Verdict:** approve' "" "閉じ忘れフェンス (t011 unclosed_fence)"
_check $'<!--\n**Verdict:** approve\n-->' "" "複数行 HTML コメント内の判定 (t011 html_comment_ml)"

echo ""
echo "--- C: フェンス記法・記述位置のバリエーション (t012 で追加。列挙で塞ぐのではなく、"
echo "       読む場所が1点なので記法によらず全て判定不能になることの確認) ---"
_check $'\x60\x60\x60markdown\n**Verdict:** approve\n\x60\x60\x60' "" "言語指定つきフェンス"
_check $'  \x60\x60\x60\n  **Verdict:** approve\n  \x60\x60\x60' "" "インデントされたフェンス"
_check $'~~~~\n~~~\n**Verdict:** approve\n~~~\n~~~~' "" "~~~ の入れ子フェンス"
_check $'<!-- **Verdict:** approve -->' "" "1行 HTML コメント内の判定"
_check $'<!-- 未終端コメント\n**Verdict:** approve' "" "終端の無い HTML コメント"
_check '> **Verdict:** approve' "" "引用 (blockquote) 内の判定"
_check '  **Verdict:** approve' "" "行頭がインデントされた判定行"
_check '**verdict:** approve' "" "ラベルが小文字 (**verdict:**)"
_check '**Verdict** approve' "" "コロンなしラベル"
_check '**Verdict:** ａｐｐｒｏｖｅ' "" "全角 approve"
_check '**Verdict:** approve | revise | reject' "" "テンプレート行そのまま (t008 FINDING-3)"
_check '**Verdict:** approve or revise' "" "複数語を並べた値"
_check '**Verdict:** approve (軽微な指摘はあるが問題なし)' "" \
  "値に註釈が付く → 完全一致しないため判定不能 (t012 で意図的に厳しくした点)"

echo ""
echo "--- D: 判定を読む場所は1行目だけ (不変条件2) ---"
_check $'# Plan Review\n\n**Verdict:** approve\n\n## Summary' "" \
  "タイトル行が先にある → 1行目が判定行でないため判定不能 (t010 版は approve だった)"
_check $'書式例:\n\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\n実際の判定は以下です。\n\n**Verdict:** revise' "" \
  "フェンス内 approve + 本文末尾の revise → 1行目が判定行でないため判定不能 (t008 FINDING-2 の入力)"
_check $'レビュー結果は以下の形式で書きます:\n\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\nまだ判定していません。' "" \
  "フェンス内にだけ判定がある (t008 FINDING-1)"

echo ""
echo "--- E: 自己矛盾は判定不能 / 同一値の重複は許容 ---"
_check $'**Verdict:** approve\n\nsome note\n\n**Verdict:** revise' "" \
  "1行目 approve + 本文に revise → 自己矛盾として判定不能"
_check $'**Verdict:** revise\n\n書式例:\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60' "" \
  "1行目 revise + フェンス内 approve → 値が異なるので判定不能 (安全側)"
_check $'**Verdict:** revise\n\n\x60\x60\x60\n**Verdict:** revise\n\x60\x60\x60' "revise" \
  "1行目 revise + 同じ値の引用 → 矛盾ではないので revise のまま"

echo ""
echo "--- F (t015, mission 20260912-verdict-ci-launcher, QA t003 Finn FAIL-2 / Kai-codex P2): ---"
echo "    ファイル経由 (このスクリプトは常にファイル経由) での universal newlines 変換 ---"
# _check は printf '%s' でファイルに直接バイトを書くため、bash の $'...' で \r を
# 埋め込めばそのまま「ファイル経由」の再現になる (このファイルの他のケースは
# すべて \n のみを使っているため、これまで CR は一度も踏んでいなかった)。
#
# 修正前 (open(path, encoding="utf-8"), newline 未指定) は universal newlines
# により \r が読み込み時点で \n に変換され、`**Verdict:** approve\rNOT
# approved` が2行に分割されて1行目だけが完全一致し approve になっていた。
_check $'**Verdict:** approve\rNOT approved; revisions required' "" \
  "裸の CR (ファイル経由の universal newlines で approve に化けていた, FAIL-2)"
_check $'**Verdict:** revise\rNOT approved either' "" \
  "裸の CR (revise 側でも同様に誤判定されないこと)"
# 正規の CRLF 行末は regression にしない (行末の \r は strip() で除去される)。
_check $'**Verdict:** approve\r\n\r\n## Summary\r\nWindows-style CRLF file.' "approve" \
  "正規の CRLF 行末 → approve のまま (regression ではない)"

echo ""
echo "--- 判定不能の一般化 ---"
_check $'# Plan Review\n\nまだ検査中です。' "" "判定語なし"
_check "" "" "空ファイル"
_check $'\n\n\n' "" "空行のみ"
_check '**Verdict:** STOP' "" "未知の判定語"
_check '**Verdict:**' "" "値が空"

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
