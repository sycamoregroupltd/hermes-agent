#!/usr/bin/env bash
# One-shot wake for sycode-trading t_5a1fbb5e (Q3->Q4 promotion review, resume-at 2026-10-03T00:00Z).
# Added 2026-09-22 by Claude Code desk (Frank-directed): kanban-scheduled-wake-scanner is paused.
# Fail-closed: refuses to fire before the resume date. Remove after it has run.
set -euo pipefail
[ "$(date -u +%Y%m%d)" -ge 20261003 ] || { echo "too early; not waking t_5a1fbb5e"; exit 0; }
/home/frank/.local/bin/hermes kanban --board sycode-trading unblock t_5a1fbb5e
echo "woke t_5a1fbb5e for Q3->Q4 promotion review"
