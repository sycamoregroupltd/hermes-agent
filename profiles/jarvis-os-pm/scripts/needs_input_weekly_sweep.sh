#!/bin/bash
# needs_input_weekly_sweep.sh -- wrapper for no_agent cron
# Calls needs_input_reporter.py --create-card
# See needs_input_escalation.sh for rationale on why a wrapper is needed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/needs_input_reporter.py" --create-card
