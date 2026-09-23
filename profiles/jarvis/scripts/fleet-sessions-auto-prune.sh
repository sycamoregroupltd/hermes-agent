#!/usr/bin/env bash
# Isolation-safe: prune ended sessions older than 30d across lean profiles.
# Mirrors sessions.auto_prune config; does NOT hermes update / restart gateways.
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/$(id -u)
PROFILES=(jarvis buzzgw jarvis-voice yorkstone-supplies-pm fleet-engineer builder devops research os-architect os-reviewer)
for p in "${PROFILES[@]}"; do
  home="/home/frank/.hermes/profiles/$p"
  [ -d "$home" ] || continue
  echo "=== $p ==="
  HERMES_HOME="$home" hermes sessions prune --older-than 30d --yes 2>&1 | tail -5 || true
done
echo DONE $(date -Is)
