#!/usr/bin/env bash
# Wrapper for self-improvement-harvester cron job.
# Delegates to the canonical script in the sycode-trading repo.
# (Previously cd'd into the deleted worktree .worktrees/t_9a2cdb2d —
# fixed 2026-08-22, t_942c4570, to use the canonical repo path.)
set -euo pipefail

cd /home/frank/sycode-trading/tools/self-improvement-harvester
exec python3 self-improvement-harvester.py \
  --lookback-seconds 3600 \
  --json \
  --board sycode-trading \
  --board jarvis-os
