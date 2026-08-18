#!/usr/bin/env bash
# Fleet Hermes updater.
# origin is NousResearch/hermes-agent. This tree carries a small overlay
# (kanban/cron survival patches). Stock `hermes update` fast-forward-fails
# against that overlay and then `reset --hard origin/main`, which drops the
# overlay (happened 2026-08-14: live jumped to tag v2026.8.13).
#
# This script MERGES Nous main into fleet/live, then reinstalls deps.
# Never reset --hard to origin.
set -euo pipefail

LIVE="${HERMES_AGENT_ROOT:-/home/frank/.hermes/hermes-agent}"
MARKER="${HERMES_HOME:-/home/frank/.hermes}/FLEET-OVERLAY"
BRANCH="fleet/live"
NOUS_REMOTE="origin"
NOUS_BRANCH="main"
export HERMES_FLEET_UPDATE=1

if [[ ! -d "$LIVE/.git" && ! -f "$LIVE/.git" ]]; then
  echo "✗ $LIVE is not a git checkout" >&2
  exit 1
fi

git_live() { git -C "$LIVE" "$@"; }

check_only=0
for arg in "$@"; do
  case "$arg" in
    --check) check_only=1 ;;
  esac
done

# Refuse to run unless origin is Nous.
origin_url="$(git_live remote get-url "$NOUS_REMOTE" 2>/dev/null || true)"
case "$origin_url" in
  *NousResearch/hermes-agent*) ;;
  *)
    echo "✗ $NOUS_REMOTE is not NousResearch/hermes-agent (got: ${origin_url:-unset})" >&2
    echo "  Fix: git -C $LIVE remote set-url origin https://github.com/NousResearch/hermes-agent.git" >&2
    exit 2
    ;;
esac

echo "⚕ Fleet update (merge Nous, keep overlay)"
echo "  checkout: $LIVE"
echo "  origin:   $origin_url"
echo

echo "→ Fetching $NOUS_REMOTE/$NOUS_BRANCH ..."
git_live fetch --quiet "$NOUS_REMOTE" "$NOUS_BRANCH"

nous_tip="$(git_live rev-parse --short "$NOUS_REMOTE/$NOUS_BRANCH")"
head_now="$(git_live rev-parse --short HEAD)"
behind="$(git_live rev-list --count "HEAD..$NOUS_REMOTE/$NOUS_BRANCH")"
ahead="$(git_live rev-list --count "$NOUS_REMOTE/$NOUS_BRANCH..HEAD")"

echo "  HEAD     $head_now"
echo "  Nous     $nous_tip  (behind=$behind overlay-ahead=$ahead)"

if [[ "$check_only" -eq 1 ]]; then
  if [[ "$behind" -eq 0 ]]; then
    echo "✓ Overlay already contains latest Nous $NOUS_BRANCH."
  else
    echo "⚕ $behind commit(s) on Nous $NOUS_BRANCH to merge."
    echo "  Run: hermes update"
  fi
  exit 0
fi

# Stay on the overlay branch. Recreate it if we are detached or on a tag.
current_branch="$(git_live rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  if git_live show-ref --verify --quiet "refs/heads/$BRANCH"; then
    # Another worktree may own the branch; then stay detached at current HEAD
    # but still merge into this checkout by committing on detached HEAD.
    if git_live worktree list | grep -q "\[$BRANCH\]" && \
       ! git_live worktree list | grep -q "^$LIVE .*\[$BRANCH\]"; then
      echo "  ⚠ branch $BRANCH is checked out in another worktree; merging on detached HEAD"
    else
      echo "→ Checking out $BRANCH"
      git_live checkout -q "$BRANCH"
    fi
  else
    echo "→ Creating $BRANCH at $head_now"
    git_live checkout -q -B "$BRANCH"
  fi
fi

if [[ "$behind" -eq 0 ]]; then
  echo "✓ Already contains latest Nous. Refreshing deps..."
else
  echo "→ Merging $NOUS_REMOTE/$NOUS_BRANCH ($behind new commit(s))..."
  if ! git_live merge --no-edit --no-ff "$NOUS_REMOTE/$NOUS_BRANCH"; then
    echo "✗ Merge conflict applying latest Nous onto the fleet overlay." >&2
    echo "  Resolve in $LIVE, then: git merge --continue && uv pip install -e '.[all]'" >&2
    echo "  Or abort: git merge --abort" >&2
    exit 3
  fi
fi

echo "→ Reinstalling Python package into the live venv..."
uv pip install -e ".[all]" --python "$LIVE/venv/bin/python"

ver="$("$LIVE/venv/bin/python" -c 'import hermes_cli; print(hermes_cli.__version__, getattr(hermes_cli, "__release_date__", ""))')"
echo
echo "✓ Fleet update complete: Hermes $ver"
echo "  HEAD $(git_live rev-parse --short HEAD) on $(git_live rev-parse --abbrev-ref HEAD)"
if [[ -f "$MARKER" ]]; then
  echo "  overlay marker: $MARKER"
fi
echo
echo "  Restart always-on gateways to load the new code:"
echo "    systemctl --user restart hermes-gateway-jarvis.service hermes-gateway-jarvis-os-pm.service hermes-gateway-jarvis-voice.service hermes-gateway-sycode-trading-pm.service hermes-gateway-trading-devops.service"
