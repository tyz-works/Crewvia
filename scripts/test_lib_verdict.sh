#!/usr/bin/env bash
# scripts/test_lib_verdict.sh
# scripts/lib_verdict.py の回帰テスト
# (t010 で新規作成 / t012 で不変条件の変更に合わせて全面改訂,
#  mission 20260909-dead-config-sweep, QA t008 FINDING-1/2/3 → QA t011 NEW-1/2 /
#  t018 で 3 状態化, mission 20260912-verdict-ci-launcher, QA t016 FAIL-A)。
#
# 不変条件 (t012):
#   1. 値は完全一致 allowlist (approve/revise/reject の1語ちょうど、小文字)
#   2. 判定を読む場所はファイルの最初の非空行のみ (全体走査で採らない)
#   3. フェンス/HTML コメントの除去ヒューリスティクスを使わない
#
# t018 で追加した不変条件 (Director 設計判断1-4):
#   4. 結果は 3 状態で、CLI の終了コードで区別する
#        VALID     = exit 0  + 標準出力に 1 語
#        NO_SIGN   = exit 10 + 標準出力なし (verdict 行の兆候がどこにも無い)
#        VIOLATION = exit 20 + 標準出力なし (兆候はあるが VALID でない)
#   5. VALID は「兆候のある行がちょうど 1 行、それが最初の非空行、値が正規」
#      だけ。兆候が 2 行以上なら値が同じでも VIOLATION
#   6. 兆候 = 正規化 (NFKD・casefold・空白/*/_/`/\/書式文字/結合文字の除去)
#      した行に `verdict:` が含まれること。記法を問わず広く拾う
#      (救済を拒否する方向にしか使わないため)
#
# 検証内容:
#   A. 正常系 (canon approve/revise/reject) に regression がないこと
#   B. QA t011 が実測した危険側入力 (t012 までは「判定不能」、t018 で VIOLATION)
#   C. フェンス記法・記述位置のバリエーション
#   D. 判定を書く場所が1行目でない場合
#   E. 自己矛盾 / 同じ値の重複 (t018 で VIOLATION に変更)
#   F. ファイル経由の universal newlines 変換 (t015)
#   G. t018: 兆候の有無で NO_SIGN と VIOLATION を分けること (QA t016 B_k1〜B_k5 等)
#   H. t018: ファイルとして読めない場合の状態
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

echo "== test_lib_verdict.sh (t010 → t012 全面改訂 → t018 3 状態化) =="

# $1 = パス, $2 = 期待 (approve|revise|reject|NO_SIGN|VIOLATION|USAGE), $3 = ラベル
_check_path() {
  local f="$1" expected="$2" label="$3"
  local want_rc want_out out rc=0
  case "$expected" in
    NO_SIGN)   want_rc=10; want_out="" ;;
    VIOLATION) want_rc=20; want_out="" ;;
    *)         want_rc=0;  want_out="$expected" ;;
  esac
  out="$(python3 "$LIB" "$f" 2>/dev/null)" || rc=$?
  if [[ "$rc" -eq "$want_rc" && "$out" == "$want_out" ]]; then
    pass "$label → $expected (rc=$rc)"
  else
    fail "$label → $expected (rc=$want_rc out='$want_out') を期待したが rc=$rc out='$out'"
  fi
}

# $1 = ファイル内容 (バイト列そのまま書く), $2 = 期待, $3 = ラベル
_check() {
  local content="$1" expected="$2" label="$3"
  local f="$TMPDIR_TEST/case.md"
  printf '%s' "$content" > "$f"
  _check_path "$f" "$expected" "$label"
}

echo ""
echo "--- A: 正常系 (regression) ---"
_check '**Verdict:** approve' "approve" "canon approve"
_check '**Verdict:** revise' "revise" "canon revise"
_check '**Verdict:** reject' "reject" "canon reject"
_check $'\n\n**Verdict:** approve\n\n## Summary\n問題なし。' "approve" "先頭に空行、1行目が判定 + 後続本文あり"
_check $'**Verdict:**\tapprove  ' "approve" "値の前後の空白/タブのみ → 許容"
_check $'**Verdict:** approve\n\n# Plan Review: m\n\n## Issues\n(verdict が revise/reject の場合のみ記載)\n' "approve" \
  "雛形の注記 '(verdict が revise/reject の場合のみ記載)' が残っていても兆候にしない (verdict の直後がコロンでない)"

echo ""
echo "--- B: QA t011 が実測した危険側入力 (t018: 兆候があるので VIOLATION) ---"
_check '**Verdict:** not approve' "VIOLATION" "not approve (t011 NEW-1)"
_check '**Verdict:** pending — do not approve yet' "VIOLATION" "pending — do not approve yet (t011 NEW-1)"
_check '**Verdict:** approve できません' "VIOLATION" "approve できません (t011 既存穴 cannot_approve_ja)"
_check '**Verdict:** approved' "VIOLATION" "approved (過去形 — 完全一致しない)"
_check '**Verdict:** APPROVE' "VIOLATION" "APPROVE (t011 NEW-3)"
_check '**Verdict:** Approve' "VIOLATION" "Approve (同上)"
_check $'A\n\x60\x60\x60\n**Verdict:** revise\n\x60\x60\x60\n\x60\x60\x60\n**Verdict:** approve' "VIOLATION" \
  "3連バッククォートが奇数個 (t011 NEW-2)"
_check $'\x60\x60\x60\x60\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\x60\x60\x60\x60' "VIOLATION" "入れ子フェンス (t011 nested_fence)"
_check $'~~~\n**Verdict:** approve\n~~~' "VIOLATION" "~~~ フェンス (t011 tilde_fence_only)"
_check $'\x60\x60\x60\n**Verdict:** approve' "VIOLATION" "閉じ忘れフェンス (t011 unclosed_fence)"
_check $'<!--\n**Verdict:** approve\n-->' "VIOLATION" "複数行 HTML コメント内の判定 (t011 html_comment_ml)"

echo ""
echo "--- C: フェンス記法・記述位置のバリエーション (記法によらず、兆候があれば VIOLATION) ---"
_check $'\x60\x60\x60markdown\n**Verdict:** approve\n\x60\x60\x60' "VIOLATION" "言語指定つきフェンス"
_check $'  \x60\x60\x60\n  **Verdict:** approve\n  \x60\x60\x60' "VIOLATION" "インデントされたフェンス"
_check $'~~~~\n~~~\n**Verdict:** approve\n~~~\n~~~~' "VIOLATION" "~~~ の入れ子フェンス"
_check $'<!-- **Verdict:** approve -->' "VIOLATION" "1行 HTML コメント内の判定"
_check $'<!-- 未終端コメント\n**Verdict:** approve' "VIOLATION" "終端の無い HTML コメント"
_check '> **Verdict:** approve' "VIOLATION" "引用 (blockquote) 内の判定"
_check '  **Verdict:** approve' "VIOLATION" "行頭がインデントされた判定行"
_check '**verdict:** approve' "VIOLATION" "ラベルが小文字 (**verdict:**)"
_check '**Verdict:** ａｐｐｒｏｖｅ' "VIOLATION" "全角 approve"
_check '**Verdict:** approve | revise | reject' "VIOLATION" "テンプレート行そのまま (t008 FINDING-3)"
_check '**Verdict:** approve or revise' "VIOLATION" "複数語を並べた値"
_check '**Verdict:** approve (軽微な指摘はあるが問題なし)' "VIOLATION" "値に註釈が付く"
_check '**Verdict** approve' "NO_SIGN" \
  "コロンなしラベル → 兆候にしない (意図的な残余: 構造化出力が唯一の判定 unit になる)"

echo ""
echo "--- D: 判定を読む場所は1行目だけ (不変条件2) ---"
_check $'# Plan Review\n\n**Verdict:** approve\n\n## Summary' "VIOLATION" \
  "タイトル行が先にある → 兆候が 1 行目以外にあるので VIOLATION (t018: 救済しない)"
_check $'書式例:\n\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\n実際の判定は以下です。\n\n**Verdict:** revise' "VIOLATION" \
  "フェンス内 approve + 本文末尾の revise (t008 FINDING-2 の入力)"
_check $'レビュー結果は以下の形式で書きます:\n\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60\n\nまだ判定していません。' "VIOLATION" \
  "フェンス内にだけ判定がある (t008 FINDING-1)"

echo ""
echo "--- E: 自己矛盾 / 同じ値の重複 (t018: どちらも VIOLATION) ---"
_check $'**Verdict:** approve\n\nsome note\n\n**Verdict:** revise' "VIOLATION" \
  "1行目 approve + 本文に revise → 自己矛盾"
_check $'**Verdict:** revise\n\n書式例:\n\x60\x60\x60\n**Verdict:** approve\n\x60\x60\x60' "VIOLATION" \
  "1行目 revise + フェンス内 approve"
_check $'**Verdict:** revise\n\n\x60\x60\x60\n**Verdict:** revise\n\x60\x60\x60' "VIOLATION" \
  "1行目 revise + 同じ値の引用 → t018 で VIOLATION に変更 (兆候は値によらず 1 行だけ)"
_check $'**Verdict:** approve\n\nnote\n\n**Verdict:** approve' "VIOLATION" \
  "同じ approve が複数行 → t018 で VIOLATION に変更 (Director 設計判断2: 複数あって値が同じ も含む)"

echo ""
echo "--- F (t015, QA t003 Finn FAIL-2 / Kai-codex P2): ファイル経由での universal newlines 変換 ---"
_check $'**Verdict:** approve\rNOT approved; revisions required' "VIOLATION" \
  "裸の CR (ファイル経由の universal newlines で approve に化けていた, FAIL-2)"
_check $'**Verdict:** revise\rNOT approved either' "VIOLATION" \
  "裸の CR (revise 側)"
_check $'**Verdict:** approve\r\n\r\n## Summary\r\nWindows-style CRLF file.' "approve" \
  "正規の CRLF 行末 → approve のまま (regression ではない)"

echo ""
echo "--- G (t018, QA t016 Finn FAIL-A / Kai-codex P1): 兆候の有無で NO_SIGN と VIOLATION を分ける ---"
_check '## 総合判定: GO' "NO_SIGN" "兆候なしの別表記 → NO_SIGN (構造化出力による救済は残す)"
_check $'# Plan Review\n\n## Summary\n問題なし。' "NO_SIGN" "判定語なしの本文 → NO_SIGN"
_check $'**Verdict:** revise\n\n書式例として:\n\n**Verdict:** approve' "VIOLATION" "B_k1: 1行目 revise + 本文に approve"
_check $'**Verdict:** approve\n\n**Verdict:** revise' "VIOLATION" "B_k2: 1行目 approve + 本文に revise"
_check '**Verdict:** revise (重大な指摘あり)' "VIOLATION" "B_k3: 値に註釈"
_check '**Verdict:** REVISE' "VIOLATION" "B_k4: 大文字"
_check $'**Verdict:** reject\n\n**Verdict:** approve' "VIOLATION" "B_k5: 1行目 reject + 本文に approve"
_check $'**Verdict:** revise\n\n> **Verdict:** approve' "VIOLATION" "1行目 revise + blockquote 内 approve"
_check $'**Verdict:** revise\n\n    **Verdict:** approve' "VIOLATION" "1行目 revise + インデント内 approve"
_check $'**Verdict:** revise\n\n- **Verdict:** approve' "VIOLATION" "1行目 revise + リスト内 approve"
_check $'**Verdict:** revise\n\n本文中の \x60**Verdict:** approve\x60 という書式' "VIOLATION" "1行目 revise + インラインコード内 approve"
_check 'Verdict: revise' "VIOLATION" "太字なし 'Verdict: revise' (Director 定義の上位集合)"
_check '**Verdict**: revise' "VIOLATION" "コロンが太字の外 '**Verdict**: revise'"
_check '__Verdict:__ revise' "VIOLATION" "アンダースコア太字"
_check '**VERDICT:** revise' "VIOLATION" "ラベルが大文字"
_check '＊＊Ｖｅｒｄｉｃｔ：＊＊ revise' "VIOLATION" "全角ラベル (NFKD で正規化)"
_check $'**Ver\xe2\x80\x8bdict:** revise' "VIOLATION" "ラベルにゼロ幅空白 (書式文字を除去)"
_check $'\xef\xbb\xbf**Verdict:** approve' "VIOLATION" "先頭に BOM → 1行目が規定形式と完全一致しない (安全側)"
_check '**Verdict:** STOP' "VIOLATION" "未知の判定語"
_check '**Verdict:**' "VIOLATION" "値が空"

echo ""
echo "--- H (t018): 読み取りの状態 ---"
_check "" "NO_SIGN" "空ファイル"
_check $'\n\n\n' "NO_SIGN" "空行のみ"
_check_path "$TMPDIR_TEST/does-not-exist.md" "NO_SIGN" "ファイルが存在しない → NO_SIGN (reviewer が書かなかった)"
mkdir -p "$TMPDIR_TEST/a-directory.md"
_check_path "$TMPDIR_TEST/a-directory.md" "VIOLATION" "ディレクトリ (読めない) → 兆候が無いことを確認できないので VIOLATION"
_check $'## 総合判定: GO\n\xff\xfe' "VIOLATION" \
  "UTF-8 として不正なバイト列 → VIOLATION (t015 までは未捕捉例外の rc=1 で救済経路に入っていた)"
USAGE_RC=0
python3 "$LIB" >/dev/null 2>&1 || USAGE_RC=$?
if [[ "$USAGE_RC" -eq 2 ]]; then
  pass "引数なし → exit 2 (usage)"
else
  fail "引数なし → exit 2 を期待したが rc=$USAGE_RC"
fi

echo ""
echo "== Results: $PASS_COUNT passed, $FAIL_COUNT failed =="
[[ "$FAIL_COUNT" -eq 0 ]]
