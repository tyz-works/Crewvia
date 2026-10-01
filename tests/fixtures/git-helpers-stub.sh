# crewvia test stub: git-helpers
# 隔離コピー (copy_plan_tree) の scripts/git-helpers.sh として写される**偽物**。git を呼ばない。
# 本物は「不在 = pull が needs_director」(GIT-05) なので、隔離テストは「不在」ではなくこの stub で
# 本物の repo に worktree を作らない継ぎ目を作る (knowledge/git-policy.md §1.4)。
# fixture の root は自分の位置 (scripts/ の親) から決める。git に聞かないので、本物の repo には届かない。

crewvia_create_worktree() {
  local mission_slug="${1:-}" task_id="${2:-}" task_slug="${3:-}"
  if [[ -z "$mission_slug" || -z "$task_id" || -z "$task_slug" ]]; then
    echo "crewvia_create_worktree (stub): W5: mission_slug, task_id, task_slug are required" >&2
    return 1
  fi
  local root
  root="$(cd -P -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)" || return 1
  local wt="${root}/.claude/worktrees/${mission_slug}/${task_id}-${task_slug}"
  mkdir -p "$wt" || return 1
  echo "$wt"
}
