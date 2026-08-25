#!/usr/bin/env bash
# In-dir cron shim for the self-improvement harvester (t_942c4570).
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths
# (see trading-devops SOUL / scheduler.py), so this REAL in-dir shim must
# exec the canonical harvester living in the sycode-trading repo. Canonical:
#   /home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py
# Dead-pin history: the old job pointed at a deleted worktree
#   (sycode-trading/.worktrees/t_9a2cdb2d) and a missing script — fixed by
#   pointing at the canonical repo path and installing this in-dir shim.
set -euo pipefail
exec python3 /home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py \
  --lookback-seconds 3600 \
  --json \
  --board sycode-trading \
  --board jarvis-os \
  "$@"
