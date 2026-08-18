#!/usr/bin/env bash
# Keep the LIVE Hermes install on fleet/live and keep out-of-tree hooks
# installed. Safe to run from a timer every minute. Always exit 0.
set -u
LIVE=/home/frank/.hermes/hermes-agent
HOOKS=/home/frank/.hermes/hooks/hermes-agent-live
MARKER=/home/frank/.hermes/FLEET-OVERLAY
LOG=/home/frank/.hermes/logs/hermes-live-guard.log
mkdir -p "$(dirname "$LOG")"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) $*" >>"$LOG"; }

[[ -d "$LIVE" ]] || exit 0
if [[ ! -f "$MARKER" ]]; then
  cat >"$MARKER" <<'EOF'
policy=merge-nous
branch=fleet/live
origin=https://github.com/NousResearch/hermes-agent.git
fork=git@github.com:sycamoregroupltd/hermes-agent.git
updater=/home/frank/.hermes/scripts/hermes-fleet-update.sh
EOF
  log "REPAIR recreated $MARKER"
fi

# Reinstall hooksPath if an agent unset it.
want_hooks="$HOOKS"
have_hooks=$(git -C "$LIVE" config --get core.hooksPath 2>/dev/null || true)
if [[ "$have_hooks" != "$want_hooks" ]]; then
  git -C "$LIVE" config --local core.hooksPath "$want_hooks"
  chmod +x "$HOOKS"/* 2>/dev/null || true
  log "REPAIR hooksPath was='${have_hooks:-unset}' now=$want_hooks"
fi

# Never clobber an in-progress fleet update merge.
if git -C "$LIVE" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
  exit 0
fi

branch=$(git -C "$LIVE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo UNKNOWN)
if [[ "$branch" != "fleet/live" ]]; then
  if git -C "$LIVE" show-ref --verify --quiet refs/heads/fleet/live; then
    HERMES_FLEET_UPDATE=1 git -C "$LIVE" checkout -q fleet/live
    now=$(git -C "$LIVE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo UNKNOWN)
    log "RESTORE was=$branch now=$now"
  else
    log "FAIL fleet/live ref missing; was=$branch"
  fi
fi

# Aborted `git checkout` still writes the target tree, then the hook
# rejects the ref move — working tree no longer matches fleet/live.
# Live is not a dev checkout: discard that dirt.
if [[ "$(git -C "$LIVE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)" == "fleet/live" ]]; then
  if ! grep -q completed_pending_review "$LIVE/hermes_cli/kanban_db.py" 2>/dev/null; then
    HERMES_FLEET_UPDATE=1 git -C "$LIVE" reset --hard HEAD >/dev/null
    log "RESET worktree (overlay marker missing after aborted checkout)"
  fi
fi
exit 0
