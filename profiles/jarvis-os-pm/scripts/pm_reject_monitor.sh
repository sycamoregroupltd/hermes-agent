#!/bin/bash
# pm_reject_monitor.sh -- wrapper for no_agent cron
# Calls pm_reject_monitor.py with --vault for vault persistence + stdout digest.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/pm_reject_monitor.py" --vault
