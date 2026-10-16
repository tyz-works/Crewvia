#!/usr/bin/env bash
set -euo pipefail

# git-helpers.sh — Crewvia Git ワークフローヘルパー
# Usage:
#   source scripts/git-helpers.sh
#   crewvia_create_worktree "mission-slug" "t001" "task-slug"
#   crewvia_create_pr "mission-slug" "task/mission-slug/t001-task-slug" "タイトル" "本文"
#
# branch 名・worktree の path・base・PR base は **scripts/lib_git_policy.py (Git Policy Resolver) が決める**
# (knowledge/git-policy.md §3)。このファイルは git の観測 (show-ref / worktree list) と副作用 (fetch /
# worktree add) だけを持ち、判断のコピーを持たない。Resolver が拒否したら (mission.yaml の `git:` が壊れている
# 等) 失敗する (既定値に倒さない)。queue の場所は CREWVIA_QUEUE (無ければ <repo root>/queue)。


# _crewvia_repo_root — main repo root (works in linked worktrees too)
_crewvia_repo_root() {
  local git_common_dir
  git_common_dir="$(git rev-parse --git-common-dir)"
  if [[ "$git_common_dir" != /* ]]; then
    git rev-parse --show-toplevel
  else
    dirname "$git_common_dir"
  fi
}


# _crewvia_policy_cli <verb> [args...] — Resolver の CLI を呼ぶ (Resolver は scripts/ の自分の隣にある)。
#   stdout: CLI の出力。拒否 (exit 2)・lib が無い・python が無い、はすべて return 1 (stderr に理由)。
_crewvia_policy_cli() {
  local here
  here="$(cd -P -- "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || return 1
  python3 "${here}/lib_git_policy.py" "$@"
}


# _crewvia_queue_dir <repo_root> — queue の場所。plan.sh と同じ規則 (CREWVIA_QUEUE、無ければ <repo root>/queue)。
_crewvia_queue_dir() {
  echo "${CREWVIA_QUEUE:-${1}/queue}"
}


# _crewvia_resolve_task <mission_slug> <task_id> <task_slug> <repo_root>
#   Resolver の resolve-task を 1 回呼び、5 行 (branch / worktree path / base_remote / base_local / pr_base) を
#   stdout に出す。拒否は stderr に `P1: git policy ...` を出して return 1。
_crewvia_resolve_task() {
  local out rc=0
  # 成功時の stderr は空 (Resolver は警告を出さない)。失敗時は stdout と stderr が混ざるが、末尾の 1 行が理由。
  out="$(_crewvia_policy_cli resolve-task --lines --queue "$(_crewvia_queue_dir "$4")" --mission "$1" \
           --task "$2" --task-slug "$3" --repo-root "$4" 2>&1)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    # 理由の本体 (どの欄か・何行目か・どのファイルか) は CLI の最後の 1 行。card の要約 (200 文字) に行番号が入るよう、
    # 前置きを短くして先頭に置く。
    echo "crewvia_create_worktree: P1: $(printf '%s\n' "$out" | tail -n 1 | sed 's/^lib_git_policy: //')" >&2
    return 1
  fi
  printf '%s\n' "$out"
}


# crewvia_handoff_path <agent_name> <mission_slug> <task_id>
# crewvia_handoff_path <agent_name> <task_id>            (旧形式・互換。mission を含まない)
#   Canonical absolute path for a Worker's HANDOFF.md (graceful handoff protocol).
#   新形式: registry/handoffs/<agent>/<mission>/<task_id>_HANDOFF.md — 別 mission の同じ tNNN が
#   同じファイルにならない (vNext-02 PR-B)。旧形式 (2 引数) は registry/handoffs/<agent>/<task_id>_HANDOFF.md
#   を返す。旧 path で書かれた handoff も fail の報告 / dispatcher は絶対パスなら読めるので、
#   旧呼び出し・rollback 先のコードを壊さないために残す。新規の呼び出しは 3 引数を使うこと。
#   mission_slug は lib_git_policy.check_mission_slug と同じ規則 (空・"/"・"\\"・"."・".."・制御文字は拒否)。
#   Used by the writer (agents/worker.md Handoff 手順 Step 2) to produce a single,
#   worktree-independent path. Always resolves under the MAIN repo root via
#   _crewvia_repo_root (works from inside a per-task worktree — task_158: a plain
#   relative path there resolved to two different files for writer vs reader).
#   The reader (scripts/dispatcher.sh handoff detection) cannot source this bash
#   function — that logic is embedded Python (dispatcher.sh invokes it via
#   `python3 - <<'PYEOF'`) — so it independently expects handoff_path to already
#   be absolute and warns if it ever receives one that is not. registry/handoffs/
#   is gitignored, so visibility does not depend on which branch/worktree is
#   checked out.
crewvia_handoff_path() {
  local agent_name="${1:-}" mission_slug="" task_id=""

  case $# in
    2) task_id="${2:-}" ;;
    3) mission_slug="${2:-}"; task_id="${3:-}" ;;
    *)
      echo "crewvia_handoff_path: usage: crewvia_handoff_path <agent_name> <mission_slug> <task_id>" >&2
      return 1
      ;;
  esac

  if [[ -z "$agent_name" || -z "$task_id" ]]; then
    echo "crewvia_handoff_path: agent_name, task_id are required" >&2
    return 1
  fi

  if [[ $# -eq 3 ]]; then
    if [[ -z "$mission_slug" || "$mission_slug" == */* || "$mission_slug" == *\\* \
          || "$mission_slug" == "." || "$mission_slug" == ".." \
          || "$mission_slug" =~ [[:cntrl:]] ]]; then
      echo "crewvia_handoff_path: mission_slug として使えない値です: ${mission_slug@Q}" >&2
      return 1
    fi
  fi

  local repo_root
  repo_root="$(_crewvia_repo_root)"
  if [[ $# -eq 3 ]]; then
    echo "${repo_root}/registry/handoffs/${agent_name}/${mission_slug}/${task_id}_HANDOFF.md"
  else
    echo "${repo_root}/registry/handoffs/${agent_name}/${task_id}_HANDOFF.md"
  fi
}


# _crewvia_physical_dir <path> — 存在する dir の物理パス (symlink 解決済み) を出す。できなければ return 1。
_crewvia_physical_dir() {
  [[ -d "${1:-}" ]] || return 1
  (cd -P -- "$1" 2>/dev/null && pwd -P)
}


# _crewvia_worktree_lookup <path> — `git worktree list --porcelain -z` で <path> の登録を調べる (観測だけ)。
#   stdout 1 行: `none` | `registered <ref|detached> <prunable:0|1>`。git の出力を読めなければ return 1。
#   path は両側を物理パスに正規化して完全一致で比べる。正規化できない登録は「一致しない」(= 再利用しない側)。
#   -z の出力は NUL 区切り (属性ごとに NUL、レコードの区切りは空の属性)。**NUL のまま読む** (改行に直すと、
#   改行を含む path の worktree が `worktree <別の path>` の行を偽造できる)。
_crewvia_worktree_lookup() {
  local want="${1:-}" want_real result
  want_real="$(_crewvia_physical_dir "$want")" || { echo none; return 0; }
  # git の出力を NUL のままパイプで読む (一時ファイルを使わない)。git が失敗したら pipefail で関数が失敗する。
  result="$(git worktree list --porcelain -z | {
    local field cur_path="" cur_ref="detached" cur_prun=0 found="" real
    while IFS= read -r -d '' field; do
      case "$field" in
        "")
          if [[ -n "$cur_path" && -z "$found" ]] && real="$(_crewvia_physical_dir "$cur_path")" && [[ "$real" == "$want_real" ]]; then
            found="registered ${cur_ref} ${cur_prun}"
          fi
          cur_path=""; cur_ref="detached"; cur_prun=0 ;;
        "worktree "*) cur_path="${field#worktree }" ;;
        "branch "*) cur_ref="${field#branch }" ;;
        prunable*) cur_prun=1 ;;
      esac
    done
    # 最後のレコードの後ろに区切りが無い出力でも取りこぼさない
    if [[ -n "$cur_path" && -z "$found" ]] && real="$(_crewvia_physical_dir "$cur_path")" && [[ "$real" == "$want_real" ]]; then
      found="registered ${cur_ref} ${cur_prun}"
    fi
    echo "${found:-none}"
  })" || return 1
  echo "$result"
}


# crewvia_create_worktree <mission_slug> <task_id> <task_slug>
#   Creates a worktree + branch for a Worker task, or reuses this task's own registered worktree.
#   Prints the worktree absolute path to stdout (stdout は path 1 行だけ)。
#   失敗は return 1 で、stderr の最後の行に分類 (W3 / W4 / W5) を出す (knowledge/git-policy.md §1.3)。
#     W2 path がある・登録済み・この task の branch・prunable でない → 再利用 (何も変えない)
#     W3 path がある・登録済みだが別の branch / detached → 失敗
#     W4 path がある・登録されていない dir / 壊れた登録 → 失敗 (dir を消さない)
#     W5 作れない (親 dir が書けない・branch が別の worktree で使用中・base が無い ...) → 失敗
crewvia_create_worktree() {
  local mission_slug="${1:-}"
  local task_id="${2:-}"
  local task_slug="${3:-}"

  if [[ -z "$mission_slug" || -z "$task_id" || -z "$task_slug" ]]; then
    echo "crewvia_create_worktree: W5: mission_slug, task_id, task_slug are required" >&2
    return 1
  fi

  local repo_root
  repo_root="$(_crewvia_repo_root)" || { echo "crewvia_create_worktree: W5: cannot determine the repo root" >&2; return 1; }
  # branch・path・base の候補は Resolver から得る (式をここに持たない)。拒否は P1 で失敗する。
  local resolved branch worktree_path base_remote base_local _pr_base
  resolved="$(_crewvia_resolve_task "$mission_slug" "$task_id" "$task_slug" "$repo_root")" || return 1
  { IFS= read -r branch; IFS= read -r worktree_path; IFS= read -r base_remote; IFS= read -r base_local; IFS= read -r _pr_base; } <<<"$resolved"
  if [[ -z "$branch" || -z "$worktree_path" || -z "$base_remote" || -z "$base_local" ]]; then
    echo "crewvia_create_worktree: P1: Resolver の出力が不完全です" >&2
    return 1
  fi

  if [[ -e "$worktree_path" || -L "$worktree_path" ]]; then
    local found
    found="$(_crewvia_worktree_lookup "$worktree_path")" || {
      echo "crewvia_create_worktree: W4: cannot read git worktree list, not reusing: $worktree_path" >&2
      return 1
    }
    if [[ "$found" == registered* ]]; then
      local _tag ref prun
      read -r _tag ref prun <<<"$found"
      if [[ "$ref" == "refs/heads/${branch}" && "$prun" == "0" && -d "$worktree_path" && ! -L "$worktree_path" ]]; then
        echo "crewvia_create_worktree: reusing registered worktree $worktree_path ($branch)" >&2
        echo "$worktree_path"
        return 0
      fi
      echo "crewvia_create_worktree: W3: path is a worktree of ${ref} (prunable=${prun}), not ${branch}: $worktree_path" >&2
      return 1
    fi
    echo "crewvia_create_worktree: W4: path exists but is not a registered worktree: $worktree_path" >&2
    return 1
  fi

  git fetch origin 2>/dev/null || echo "warning: git fetch origin failed, using local state" >&2

  # base を選ぶ 1 行 (規則: remote-tracking があれば remote、無ければ local に倒して警告。lib_git_policy.task_base と同じ。
  # 観測は git に任せる)。候補の名前は Resolver が決める。
  local base="$base_remote"
  if ! git show-ref --verify --quiet "refs/remotes/${base_remote}"; then
    base="$base_local"
    echo "warning: ${base_remote} not found, falling back to local ${base_local}" >&2
  fi

  mkdir -p "$(dirname "$worktree_path")" || {
    echo "crewvia_create_worktree: W5: cannot create the parent dir of $worktree_path" >&2
    return 1
  }

  if git show-ref --verify --quiet "refs/heads/${branch}"; then
    git worktree add "$worktree_path" "$branch" >&2 || {
      echo "crewvia_create_worktree: W5: git worktree add failed for existing branch ${branch}" >&2
      return 1
    }
  else
    git worktree add -b "$branch" "$worktree_path" "$base" >&2 || {
      echo "crewvia_create_worktree: W5: git worktree add -b failed for ${branch} from ${base}" >&2
      return 1
    }
  fi

  echo "$worktree_path"
}


# crewvia_remove_worktree <mission_slug> <task_id> <task_slug>
#   Removes a Worker task worktree (idempotent, branch retained).
crewvia_remove_worktree() {
  local mission_slug="${1:-}"
  local task_id="${2:-}"
  local task_slug="${3:-}"

  if [[ -z "$mission_slug" || -z "$task_id" || -z "$task_slug" ]]; then
    echo "crewvia_remove_worktree: mission_slug, task_id, task_slug are required" >&2
    return 1
  fi

  local repo_root resolved worktree_path _b
  repo_root="$(_crewvia_repo_root)"
  resolved="$(_crewvia_resolve_task "$mission_slug" "$task_id" "$task_slug" "$repo_root")" || return 1
  { IFS= read -r _b; IFS= read -r worktree_path; } <<<"$resolved"

  if ! git worktree list --porcelain | grep -qF "worktree ${worktree_path}"; then
    echo "crewvia_remove_worktree: worktree not found, skipping: $worktree_path" >&2
    return 0
  fi

  git worktree remove --force "$worktree_path"
}


# crewvia_create_pr <mission_slug> <branch> <title> <body>
#   Pushes the branch and opens a PR against the mission's PR base (Resolver の pr_base。既定は main)。
#   Prints the PR URL to stdout. PR base を決められなければ (mission.yaml の `git:` が壊れている・mission が無い)
#   push も PR 作成もせず return 1 (main に倒さない)。
crewvia_create_pr() {
  local mission_slug="${1:-}" branch="${2:-}" title="${3:-}" body="${4:-}"
  if [[ -z "$mission_slug" || -z "$branch" ]]; then
    echo "crewvia_create_pr: mission_slug, branch are required" >&2
    return 1
  fi

  local repo_root pr_base_line pr_base_value
  repo_root="$(_crewvia_repo_root)" || return 1
  pr_base_line="$(_crewvia_policy_cli pr-base --lines --queue "$(_crewvia_queue_dir "$repo_root")" --mission "$mission_slug")" || {
    echo "crewvia_create_pr: PR base を決められません (mission ${mission_slug})" >&2
    return 1
  }
  pr_base_value="$pr_base_line"

  git push -u origin "$branch"

  local pr_url
  pr_url=$(gh pr create \
    --title "$title" \
    --body "$body" \
    --base "$pr_base_value" \
    --head "$branch")

  echo "$pr_url"
}
