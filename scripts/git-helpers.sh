#!/usr/bin/env bash
set -euo pipefail

# git-helpers.sh — Crewvia Git ワークフローヘルパー
# Usage:
#   source scripts/git-helpers.sh
#   crewvia_create_branch "072" "git-workflow"
#   crewvia_create_pr "task/072-git-workflow" "タイトル" "本文"


# crewvia_create_branch <task_id> <slug>
#   Creates (or checks out) a branch named task/<task_id>-<slug>.
#   Prints the branch name to stdout.
crewvia_create_branch() {
  local task_id="$1"
  local slug="$2"
  local branch="task/${task_id}-${slug}"

  git fetch origin
  git checkout main
  git pull origin main
  git checkout -b "$branch" 2>/dev/null || git checkout "$branch"

  echo "$branch"
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
