#!/bin/sh
# CANONICAL SOURCE — residual consumer wrapper for job 53d45f13ff65 (t_dd27733b).
# Cadence: every 60m. Consumer: sycode-trading kanban incident route.
# Healthy ticks silent (quiet drift monitor). Breach/ops failure fail-visible.
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/sycode_residual_monitor_route.py" --monitor drift-monitor "$@"
