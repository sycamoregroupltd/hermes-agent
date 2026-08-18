#!/usr/bin/env bash
# Create/list worktrees for Hermes-agent work. NEVER uses the live install
# checkout as a working tree for development.
#
#   hermes-worktree add <name> [start-point]
#   hermes-worktree list
#   hermes-worktree remove <name>
#
# start-point default: origin/main (current Nous). Use fleet/live only when
# the task is a pile-3 overlay patch (kanban/cron survival).
set -euo pipefail

LIVE=/home/frank/.hermes/hermes-agent
ROOT=/home/frank/.hermes-worktrees
usage() {
  echo "usage: hermes-worktree add <name> [start-point]" >&2
  echo "       hermes-worktree list" >&2
  echo "       hermes-worktree remove <name>" >&2
  exit 2
}

cmd="${1:-}"
[[ -n "$cmd" ]] || usage
shift || true

case "$cmd" in
  list)
    git -C "$LIVE" worktree list
    ;;
  add)
    name="${1:-}"
    start="${2:-origin/main}"
    [[ -n "$name" ]] || usage
    case "$name" in
      *..*|/*|*\\*|*[[:space:]]*) echo "bad name: $name" >&2; exit 2 ;;
    esac
    dest="$ROOT/$name"
    if [[ -e "$dest" ]]; then
      echo "exists: $dest" >&2
      exit 1
    fi
    mkdir -p "$ROOT"
    git -C "$LIVE" fetch --quiet origin main || true
    branch="wt/$name"
    if git -C "$LIVE" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$LIVE" worktree add "$dest" "$branch"
    else
      git -C "$LIVE" worktree add -b "$branch" "$dest" "$start"
    fi
    echo "WORKTREE $dest"
    echo "BRANCH   $branch"
    echo "BASE     $start"
    echo "Do all Hermes-agent edits here. Never git checkout in $LIVE."
    ;;
  remove)
    name="${1:-}"
    [[ -n "$name" ]] || usage
    dest="$ROOT/$name"
    git -C "$LIVE" worktree remove --force "$dest"
    git -C "$LIVE" branch -D "wt/$name" 2>/dev/null || true
    echo "removed $dest"
    ;;
  *)
    usage
    ;;
esac
