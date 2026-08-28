#!/bin/sh
# CANONICAL SOURCE — residual consumer wrapper for job 965b5d5d4cb4 (t_dd27733b).
# Cadence: 0 7 * * * (07:00 UTC daily). Consumer: sycode-trading kanban incident route.
# Replaces the broken pit-context-join.sh symlink.
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/sycode_residual_monitor_route.py" --monitor pit-context-join "$@"
