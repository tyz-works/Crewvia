#!/usr/bin/env bash
# lib_worktree_lock.sh — repo 単位の worktree ロック (唯一の定義)。source して使う。
#
# 同じ repo への `git worktree add` / `remove`・fetch・update-ref -d・全 worktree を列挙する git 操作
# (`worktree list`) を同時に走らせると .git/worktrees/<name>/ の作成・削除と競合し
# 「failed to read .git/worktrees/<name>/commondir」で落ちる。kai-review.sh (review の spawn) と
# git-helpers.sh (Worker の plan.sh pull) の両方がこの関数を通すことで、同じ repo への操作を直列化する。
#
#   crewvia_wt_git <repo> <git args...>   — `git -C <repo> <args>` をロックの中で実行する
#
# ロックの取り方 (ここ 1 か所):
#   - ロックファイルは <git common dir>/crewvia-worktree.lock。待ちは flock -w 120。
#   - 取れなかった (タイムアウト) ときは git を実行せず exit 99 で失敗する (fail-closed)。呼び側は
#     通常の git 失敗と同じ経路 (git-helpers.sh は W5、kai-review.sh は needs-director) へ進む。
#   - flock コマンドが無い環境 (macOS など) と git common dir が分からないときはロックなしで素通り (従来どおり)。
#
# 旧名との移行: 以前 kai-review.sh だけが <git common dir>/kai-review-worktree.lock を使っていた。
# 新旧のコードが混在する間 (旧コードで起動済みの review が走っている間) に互いを排他できるよう、
# 旧名 → 新名の順 (固定順 = デッドロックしない) で両方を取る。旧名を取るのは、旧コードの review が
# 全部入れ替わった後で消してよい (消すときは CREWVIA_WT_LEGACY_LOCK の行ごと)。

CREWVIA_WT_LOCK_NAME="crewvia-worktree.lock"
CREWVIA_WT_LEGACY_LOCK_NAME="kai-review-worktree.lock"
CREWVIA_WT_LOCK_WAIT_SECONDS="${CREWVIA_WT_LOCK_WAIT_SECONDS:-120}"

crewvia_wt_git() {
  local repo="$1"; shift
  local gcd
  gcd="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || gcd=""
  if [[ -n "$gcd" && -d "$gcd" ]] && command -v flock >/dev/null 2>&1; then
    (
      # 旧名 (fd 8) → 新名 (fd 9) の順に取る。どちらかが取れなければ git を実行しない。
      flock -w "$CREWVIA_WT_LOCK_WAIT_SECONDS" 8 || exit 99
      flock -w "$CREWVIA_WT_LOCK_WAIT_SECONDS" 9 || exit 99
      git -C "$repo" "$@"
    ) 8>>"${gcd}/${CREWVIA_WT_LEGACY_LOCK_NAME}" 9>>"${gcd}/${CREWVIA_WT_LOCK_NAME}"
  else
    git -C "$repo" "$@"
  fi
}
