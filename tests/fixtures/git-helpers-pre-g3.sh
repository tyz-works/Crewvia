#!/usr/bin/env bash
# FROZEN REFERENCE (vNext 01b G3 / t012): origin/main bd4485a (G2 merge) の scripts/git-helpers.sh を**そのまま**凍結した複製。
# G3 で本物の helper は branch / path / base を Resolver から得るようになった。このファイルは「G3 前と同じ branch・path・base で
# worktree を作る」ことを同じ入力で突き合わせる互換性テスト (tests/test_git_policy_resolver.py) の**比較元**で、
# 本番では使われない。直さない (直すと比較元でなくなる)。
set -euo pipefail

# git-helpers.sh — Crewvia Git ワークフローヘルパー
# Usage:
#   source scripts/git-helpers.sh
#   crewvia_create_worktree "mission-slug" "t001" "task-slug"
#   crewvia_create_pr "task/mission-slug/t001-task-slug" "タイトル" "本文"


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


# crewvia_handoff_path <agent_name> <task_id>
#   Canonical absolute path for a Worker's HANDOFF.md (graceful handoff protocol).
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
  local agent_name="${1:-}" task_id="${2:-}"

  if [[ -z "$agent_name" || -z "$task_id" ]]; then
    echo "crewvia_handoff_path: agent_name, task_id are required" >&2
    return 1
  fi

  local repo_root
  repo_root="$(_crewvia_repo_root)"
  echo "${repo_root}/registry/handoffs/${agent_name}/${task_id}_HANDOFF.md"
}


# _crewvia_physical_dir <path> — 存在する dir の物理パス (symlink 解決済み) を出す。できなければ return 1。
_crewvia_physical_dir() {
  [[ -d "${1:-}" ]] || return 1
  (cd -P -- "$1" 2>/dev/null && pwd -P)
}


# _crewvia_worktree_lookup <path> — `git worktree list --porcelain -z` で <path> の登録を調べる (観測だけ)。
#   stdout 1 行: `none` | `registered <ref|detached> <prunable:0|1>`。git の出力を読めなければ return 1。
#   path は両側を物理パスに正規化して完全一致で比べる。正規化できない登録は「一致しない」(= 再利用しない側)。
_crewvia_worktree_lookup() {
  local want="${1:-}" want_real listing field
  want_real="$(_crewvia_physical_dir "$want")" || { echo none; return 0; }
  listing="$(git worktree list --porcelain -z | tr '\0' '\n' | sed 's/^$/\x01/')" || return 1
  local cur_path="" cur_ref="detached" cur_prun=0 found=""
  _crewvia_flush() {
    if [[ -n "$cur_path" && -z "$found" ]]; then
      local real
      if real="$(_crewvia_physical_dir "$cur_path")" && [[ "$real" == "$want_real" ]]; then
        found="registered ${cur_ref} ${cur_prun}"
      fi
    fi
    cur_path=""; cur_ref="detached"; cur_prun=0
  }
  while IFS= read -r field; do
    case "$field" in
      $'\x01') _crewvia_flush ;;
      "worktree "*) cur_path="${field#worktree }" ;;
      "branch "*) cur_ref="${field#branch }" ;;
      prunable*) cur_prun=1 ;;
    esac
  done <<<"$listing"
  _crewvia_flush
  unset -f _crewvia_flush
  echo "${found:-none}"
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

  local branch="task/${mission_slug}/${task_id}-${task_slug}"
  local repo_root
  repo_root="$(_crewvia_repo_root)" || { echo "crewvia_create_worktree: W5: cannot determine the repo root" >&2; return 1; }
  local worktree_path="${repo_root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}"

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

  local base="origin/main"
  if ! git show-ref --verify --quiet "refs/remotes/origin/main"; then
    base="main"
    echo "warning: origin/main not found, falling back to local main" >&2
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

  local repo_root
  repo_root="$(_crewvia_repo_root)"
  local worktree_path="${repo_root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}"

  if ! git worktree list --porcelain | grep -qF "worktree ${worktree_path}"; then
    echo "crewvia_remove_worktree: worktree not found, skipping: $worktree_path" >&2
    return 0
  fi

  git worktree remove --force "$worktree_path"
}


# crewvia_create_pr <branch> <title> <body>
#   Pushes the branch and opens a PR against main.
#   Prints the PR URL to stdout.
crewvia_create_pr() {
  local branch="$1"
  local title="$2"
  local body="$3"

  git push -u origin "$branch"

  local pr_url
  pr_url=$(gh pr create \
    --title "$title" \
    --body "$body" \
    --base main \
    --head "$branch")

  echo "$pr_url"
}
