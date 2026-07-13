#!/bin/bash
# Refined audit: truly-unpushed commits (not on ANY remote ref) + dirty files
while IFS= read -r wt; do
  [ -d "$wt" ] || continue
  cd "$wt" 2>/dev/null || continue
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
  cdate=$(git log -1 --format=%cs 2>/dev/null)
  unpushed=$(git rev-list --count HEAD --not --remotes 2>/dev/null)
  dirty=$(git status --porcelain 2>/dev/null | wc -l)
  if [ "${unpushed:-0}" -gt 0 ] || [ "$dirty" -gt 0 ]; then
    echo -e "$wt\t$branch\t$cdate\tunpushed=$unpushed\tdirty=$dirty"
  fi
done < <(cd ~/sycode-trading && git worktree list --porcelain | awk '/^worktree /{print $2}')
