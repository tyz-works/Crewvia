#!/usr/bin/env bash
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


# crewvia_create_worktree <mission_slug> <task_id> <task_slug>
#   Creates a worktree + branch for a Worker task.
#   Prints the worktree absolute path to stdout.
crewvia_create_worktree() {
  local mission_slug="${1:-}"
  local task_id="${2:-}"
  local task_slug="${3:-}"

  if [[ -z "$mission_slug" || -z "$task_id" || -z "$task_slug" ]]; then
    echo "crewvia_create_worktree: mission_slug, task_id, task_slug are required" >&2
    return 1
  fi

  local branch="task/${mission_slug}/${task_id}-${task_slug}"
  local repo_root
  repo_root="$(_crewvia_repo_root)"
  local worktree_path="${repo_root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}"

  if [[ -e "$worktree_path" ]]; then
    echo "crewvia_create_worktree: path already exists: $worktree_path" >&2
    return 1
  fi

  git fetch origin 2>/dev/null || echo "warning: git fetch origin failed, using local state" >&2

  local base="origin/main"
  if ! git show-ref --verify --quiet "refs/remotes/origin/main"; then
    base="main"
    echo "warning: origin/main not found, falling back to local main" >&2
  fi

  mkdir -p "$(dirname "$worktree_path")"

  if git show-ref --verify --quiet "refs/heads/${branch}"; then
    git worktree add "$worktree_path" "$branch"
  else
    git worktree add -b "$branch" "$worktree_path" "$base"
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
