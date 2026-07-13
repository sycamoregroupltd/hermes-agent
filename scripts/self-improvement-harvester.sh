#!/usr/bin/env bash
# Wrapper for self-improvement-harvester cron job.
# Delegates to the actual script in the worktree.
set -euo pipefail

cd /home/frank/sycode-trading/.worktrees/t_9a2cdb2d
exec python3 self-improvement-harvester.py \
  --lookback-seconds 3600 \
  --json \
  --board sycode-trading \
  --board jarvis-os
