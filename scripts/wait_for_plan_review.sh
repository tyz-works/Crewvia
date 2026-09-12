#!/usr/bin/env bash
# scripts/wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds] [poll_interval]
#
# plan-reviewer が書く plan_review.md を待ち、有効な verdict が見つかったら
# 成功として返す。scripts/review-plan.sh から切り出したポーリング判定の本体
# (t002, mission 20260908-launch-reliability)。
#
# 切り出した理由: review-plan.sh 本体は `claude` CLI を spawn するため
# end-to-end ではテストしにくい。ポーリング判定だけを独立スクリプトにする
# ことで、claude を一切起動せずに以下の回帰テストができる:
#   1. 有効な verdict (規定形式 `**Verdict:** approve` 等) → 即成功
#   2. 別表記の verdict (`## 総合判定: **GO**` 等) → タイムアウト
#      (F1, t002 mission 20260912-verdict-ci-launcher: 別表記をファイル全体
#      走査で救済する旧 normalize_plan_review_verdict.py 経路は、判定は1点
#      だけを完全一致で読むという不変条件を迂回する唯一の穴だったため削除
#      した。別表記の救済は scripts/review-plan.sh の構造化出力経路
#      (`claude --json-schema`) に一本化されている — このスクリプトは
#      規定形式の有無だけを見る)
#   3. start_epoch より古い plan_review.md (前 cycle の残骸) → 無視して待ち
#      続け、新しいファイルが来なければタイムアウトすること (誤って古い
#      判定を新しい判定として採用しない。t002 で実際に発生したパターン3)
#   4. plan_review.md が一度も作られない → タイムアウトすること
#
# 呼び出し前提: review-plan.sh 側で「レビュー開始前に既存の plan_review.md を
# 削除する」処理を既に行っていること。本スクリプトの mtime チェックは
# その削除が何らかの理由 (権限・競合等) で効かなかった場合の二重の安全策
# であり、削除自体の代替ではない。
#
# 標準出力の1行目で結果種別を返す (2行目以降は人間向けログ、stderr にも複製):
#   OK             — 有効な verdict が見つかった (規定形式のみ。F1 以降、
#                    別表記の正規化はこのスクリプトの責務ではない)。
#                    lib_verdict.py が終了コード 0 かつ正規の 1 語を返したときだけ
#   TIMEOUT_FRESH  — このレビュー実行で書かれたファイルは観測できたが、
#                    最後まで有効な verdict が見つからなかった
#                    (verdict 行の兆候が無い / 書式違反・自己矛盾 のどちらも。
#                    内容自体は活かせる可能性あり)
#
# t018: 結果種別はあくまで待ちの目安であり、判定の権威ではない。
# scripts/review-plan.sh は reviewer の停止を確認してから lib_verdict.py を
# 自分で呼び直し、3 状態 (有効 / 兆候なし / 書式違反) に従って救済の可否を
# 決める。ここで書式違反を OK にしないこと (Director 設計判断4)。
#   TIMEOUT_NONE   — このレビュー実行で書かれたファイルが一度も観測できなかった
# exit code: 0 = OK, 1 = TIMEOUT (fresh/none いずれも)
#
# t011 (mission 20260908-launch-reliability, QA t009 FINDING-B) 早期打ち切り:
# reviewer が規定形式にない語彙 (例 `**STOP**`) を書いた場合、lib_verdict は
# 何度呼んでも判定不能 (exit 1) のままであり、待っても結果は変わらない。
# 「fresh なファイルの mtime が 2 回連続で変化していない (=書き込みが止まって
# いる) のに verdict が読めない」ことを検出したら、残りの max_wait を待たずに
# ループを抜けて TIMEOUT_FRESH を返す。mtime が変化し続けている間は reviewer が
# まだ書いている可能性があるため打ち切らない (誤って早すぎる打ち切りをしない
# ための安全策)。
_EARLY_BREAK_STREAK=2

# Usage: wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds=600] [poll_interval=5]
set -uo pipefail

REVIEW_OUTPUT="${1:?Usage: wait_for_plan_review.sh <review_output_path> <start_epoch> [max_wait_seconds] [poll_interval]}"
START_EPOCH="${2:?start_epoch required}"
MAX_WAIT="${3:-600}"
POLL_INTERVAL="${4:-5}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERDICT_LIB="${SCRIPT_DIR}/lib_verdict.py"

# GNU (Linux/WSL) と BSD/macOS の stat 引数差を吸収する。
_mtime_of() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

_iterations=$(( (MAX_WAIT + POLL_INTERVAL - 1) / POLL_INTERVAL ))
[ "$_iterations" -lt 1 ] && _iterations=1

_fresh_seen=0
_last_unreadable_mtime=""
_unreadable_streak=0
_i=0
while [ "$_i" -lt "$_iterations" ]; do
  _i=$((_i + 1))
  if [ -f "$REVIEW_OUTPUT" ]; then
    _mtime="$(_mtime_of "$REVIEW_OUTPUT")"
    if [ "$_mtime" -ge "$START_EPOCH" ]; then
      _fresh_seen=1
      # F2 (PR#188 t012 Seo 指摘): 以前は `^\*\*Verdict:\*\*` の存在だけを
      # 見ていたため、reviewer が `**Verdict:** STOP` のような判定語以外を
      # 書いた場合でも OK (exit 0) を返していた。呼び出し元 (scripts/plan.sh
      # cmd_review) は `(approve|revise|reject)` を要求するため、そこで
      # 「no valid verdict」として弾かれ、書式ミスなのに review cycle を
      # 消費する経路になっていた。ここも同じ判定語セットを要求するように
      # 揃え、OK 判定と最終判定の条件を一致させる。
      #
      # t010 (QA t008 FINDING-1/2/3): 以前はここで直接 grep していたが、
      # コードフェンスを除去せず・複数判定の混在も検出しなかったため
      # 誤 approve になる経路があった。scripts/plan.sh 側と全く同じ抽出
      # ロジックを scripts/lib_verdict.py に一本化し、ここも同じ関数を
      # 呼ぶことで両者の条件不一致 (root cause 3) 自体を無くす。
      #
      # t018 (mission 20260912-verdict-ci-launcher, Director 設計判断4):
      # lib_verdict.py の終了コードを allowlist で解釈する。OK にするのは
      # 「終了コード 0 かつ標準出力が正規の 1 語」だけ。書式違反・自己矛盾
      # (終了コード 20) はもちろん、想定外の終了コード (Python の未捕捉例外 = 1、
      # スクリプトが無い = 2 など) も OK にしない。どちらも「まだ書いている
      # 途中かもしれない」ので即座には諦めず、下の早期打ち切りロジックに任せる
      # (待ち・停止確認・読み取りの順序は review-plan.sh 側で t015 のまま)。
      _verdict_rc=0
      _verdict_out="$(python3 "$VERDICT_LIB" "$REVIEW_OUTPUT" 2>/dev/null)" || _verdict_rc=$?
      if [ "$_verdict_rc" -eq 0 ]; then
        case "$_verdict_out" in
          approve|revise|reject)
            echo "OK"
            echo "[wait_for_plan_review] valid verdict found in $REVIEW_OUTPUT" >&2
            exit 0
            ;;
        esac
      fi
      if [ "$_verdict_rc" -eq 10 ] && [ -z "$_verdict_out" ]; then
        _last_state="no verdict-line sign"
      else
        _last_state="verdict-line format violation / self-contradiction (lib_verdict rc=$_verdict_rc)"
      fi
      # F1 (t002, mission 20260912-verdict-ci-launcher): 以前はここで
      # normalize_plan_review_verdict.py がファイル全体を走査して別表記
      # (`## 総合判定: **GO**` 等) を救済していたが、その走査が「判定は1点
      # だけを完全一致で読む」という lib_verdict.py の不変条件を丸ごと迂回
      # する唯一の経路になっていた (blockquote で引用された前 cycle の判定を
      # 拾って誤って ready/approve に倒すケースを実測)。削除して、別表記の
      # 救済は scripts/review-plan.sh の構造化出力経路 (`claude
      # --json-schema`) に一本化した — ここでは規定形式が読めなければ
      # 素直に「判定不能」に倒す。
      #
      # 規定形式が読めなかった — reviewer がまだ書き終えていない可能性が
      # あるため即座には諦めないが、mtime が前回と同じ (書き込みが止まって
      # いる) 場合はストリークを積み、2回連続で止まっていれば早期打ち切り。
      if [ "$_mtime" = "$_last_unreadable_mtime" ]; then
        _unreadable_streak=$((_unreadable_streak + 1))
      else
        _unreadable_streak=1
        _last_unreadable_mtime="$_mtime"
      fi
      if [ "$_unreadable_streak" -ge "$_EARLY_BREAK_STREAK" ]; then
        echo "[wait_for_plan_review] $REVIEW_OUTPUT stopped changing (mtime stable across ${_unreadable_streak} polls) with no readable verdict — breaking early instead of waiting the full ${MAX_WAIT}s" >&2
        break
      fi
    fi
    # _mtime < START_EPOCH: 前 cycle の残骸。無視して待ち続ける (t002 パターン3対策)
  fi
  sleep "$POLL_INTERVAL"
done

if [ "$_fresh_seen" -eq 1 ]; then
  echo "TIMEOUT_FRESH"
  echo "[wait_for_plan_review] $REVIEW_OUTPUT was written during this run but no valid verdict could be found (last state: ${_last_state:-unknown}) — inspect it by hand, the content may still be usable without consuming another review cycle" >&2
else
  echo "TIMEOUT_NONE"
  echo "[wait_for_plan_review] $REVIEW_OUTPUT was not produced within ${MAX_WAIT}s" >&2
fi
exit 1
